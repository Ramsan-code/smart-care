import logging
import time
from django.conf import settings
from datetime import timedelta
from celery import shared_task
from django.db import transaction
from django.utils import timezone
from .models import OutboxEvent, ProcessedEvent


@shared_task
def process_event(event_id):
    if settings.RESTORE_QUARANTINE:
        return
    logging.getLogger('smartcare.outbox').info('outbox', extra={'event': 'outbox.attempt', 'outbox_id': event_id})
    try:
        return _process_event(event_id)
    except Exception:
        # Move poison work out of the due window so it cannot starve later events.
        with transaction.atomic():
            event = OutboxEvent.objects.select_for_update().get(pk=event_id)
            if event.processed_at or event.dead_lettered_at:
                return
            event.attempts += 1
            event.last_error = 'Consumer failed; inspect the registered handler and retry after repair.'
            event.failure_owner = 'operations'
            event.available_at = timezone.now() + timedelta(seconds=min(3600, 30 * 2 ** min(event.attempts, 7)))
            if event.attempts >= 8:
                event.dead_lettered_at = timezone.now()
            event.save()
        raise


def _process_event(event_id):
    hint = OutboxEvent.objects.get(pk=event_id)
    if hint.processed_at or hint.dead_lettered_at or hint.available_at > timezone.now():
        return
    # External refund I/O is intentionally outside the outbox transaction too.
    refund_status = None
    if hint.topic == 'refund.requested':
        from finance.refunds import process_refund
        refund_status = process_refund(hint.payload['obligation_id'])
    with transaction.atomic():
        event=OutboxEvent.objects.select_for_update().get(pk=event_id)
        if event.processed_at or event.dead_lettered_at or event.available_at > timezone.now(): return
        if event.topic in {
            "appointment.confirmed", "appointment.changed", "appointment.cancelled",
            "appointment.reminder", "appointment.rescheduled",
        }:
            from communications.services import consume_notification_event
            notification_event = {
                "appointment.confirmed": "confirmation",
                "appointment.changed": "changed",
                "appointment.cancelled": "cancelled",
                "appointment.reminder": "reminder",
                "appointment.rescheduled": "changed",
            }[event.topic]
            delivery = consume_notification_event(event.payload, notification_event, identity=event.key)
            if delivery and delivery.status == "failed":
                event.available_at = delivery.next_attempt_at or timezone.now()
                if delivery.next_attempt_at is None:
                    event.dead_lettered_at = timezone.now()
                    event.failure_owner = 'communications'
                    event.last_error = 'SMS retry budget exhausted.'
                event.save(update_fields=["available_at", "dead_lettered_at", "failure_owner", "last_error"])
                return
        elif event.topic == 'refund.requested':
            if refund_status not in ['refunded', 'failed', 'cancelled']:
                from finance.models import RefundObligation
                operation = RefundObligation.objects.get(pk=event.payload['obligation_id'])
                event.available_at = max(filter(None, [operation.lease_until, operation.next_attempt_at, timezone.now() + timedelta(seconds=30)]))
                if operation.next_attempt_at is None:
                    event.dead_lettered_at = timezone.now()
                    event.failure_owner = 'finance'
                    event.last_error = 'Refund requires manual provider reconciliation; balance remains reserved.'
                event.save()
                return
        elif event.topic == "payment.callback.delayed":
            from finance.services import apply_signed_callback
            apply_signed_callback(
                event.payload["body"].encode(), event.payload["signature"]
            )
        elif event.topic not in {'account.registered','configuration.changed','demo.ping','session.cancelled','payment.recorded','payment.refunded','payment.unmatched'}:
            raise ValueError('No consumer registered for this event topic.')
        ProcessedEvent.objects.get_or_create(key=event.key,defaults={'result':{'topic':event.topic,'acknowledged':True}})
        event.processed_at=timezone.now()
        event.save(update_fields=['processed_at'])


@shared_task
def dispatch_outbox():
    if settings.RESTORE_QUARANTINE:
        return
    if settings.OPERATIONS_ENABLED:
        from operations.telemetry import client
        client().set('sc:outbox', time.time(), ex=90)
    for event_id in OutboxEvent.objects.filter(processed_at__isnull=True,dead_lettered_at__isnull=True,available_at__lte=timezone.now()).order_by('available_at', 'id').values_list('id',flat=True)[:100]:
        process_event.delay(event_id)
