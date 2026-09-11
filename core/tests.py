from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.db import connections,transaction
from django.test import TestCase,TransactionTestCase,tag
from django.utils import timezone
from datetime import timedelta
from unittest.mock import patch
from django.core.exceptions import ValidationError
from accounts.models import User
from .services import audit,enqueue,execute_once
from .models import OutboxEvent,ProcessedEvent,IdempotencyRecord,AuditEvent
from .tasks import process_event

class FoundationTests(TestCase):
    def setUp(self): self.user=User.objects.create_user('test@example.test','UnitTest-strong-927!')
    def test_audit_cannot_be_rewritten_or_deleted(self):
        event=audit(self.user,'test','test')
        with self.assertRaises(ValidationError): event.save()
        with self.assertRaises(ValidationError): event.delete()
        with self.assertRaises(ValidationError): AuditEvent.objects.filter(pk=event.pk).update(action='changed')
    def test_outbox_rolls_back_with_business_transaction(self):
        try:
            with transaction.atomic(): enqueue('rollback','demo.ping',{});raise RuntimeError
        except RuntimeError: pass
        self.assertFalse(OutboxEvent.objects.filter(key='rollback').exists())
    def test_outbox_consumer_deduplicates(self):
        event=enqueue('one','demo.ping',{})
        process_event(event.pk);process_event(event.pk)
        self.assertEqual(ProcessedEvent.objects.filter(key='one').count(),1)
        event.refresh_from_db();self.assertIsNotNone(event.processed_at)
    def test_unknown_topic_is_not_silently_acknowledged(self):
        event=enqueue('unknown','unknown',{})
        with self.assertRaises(ValueError): process_event(event.pk)
        event.refresh_from_db();self.assertIsNone(event.processed_at)
    def test_idempotency_rolls_back_failed_command(self):
        def command():
            audit(self.user,'should_rollback','target');raise RuntimeError
        with self.assertRaises(RuntimeError): execute_once(self.user,'test','key',{},command)
        self.assertFalse(IdempotencyRecord.objects.exists());self.assertFalse(AuditEvent.objects.exists())

    @tag('phase7')
    def test_outbox_dispatch_only_enqueues_due_events(self):
        due = enqueue('due', 'demo.ping', {})
        future = enqueue('future', 'demo.ping', {})
        future.available_at = timezone.now() + timedelta(minutes=5)
        future.save(update_fields=['available_at'])
        done = enqueue('done', 'demo.ping', {})
        done.processed_at = timezone.now()
        done.save(update_fields=['processed_at'])
        with patch('core.tasks.process_event.delay') as delayed:
            from .tasks import dispatch_outbox
            dispatch_outbox()
        delayed.assert_called_once_with(due.pk)

    @tag('phase7')
    def test_outbox_consumer_leaves_failed_event_for_recovery(self):
        event = enqueue('recovery', 'unsupported.phase7', {})
        with self.assertRaises(ValueError):
            process_event(event.pk)
        event.refresh_from_db()
        self.assertIsNone(event.processed_at)

@tag('phase7')
class IdempotencyConcurrencyTests(TransactionTestCase):
    def test_concurrent_commands_have_one_effect_on_mariadb(self):
        user=User.objects.create_user('race@example.test','UnitTest-strong-927!')
        barrier=Barrier(2)
        def attempt():
            try:
                actor=User.objects.get(pk=user.pk);barrier.wait(timeout=10)
                return execute_once(actor,'race','same',{'value':1},lambda:{'audit_id':audit(actor,'race.effect','one').pk})
            finally: connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:attempt(),range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(AuditEvent.objects.filter(action='race.effect').count(),1)
