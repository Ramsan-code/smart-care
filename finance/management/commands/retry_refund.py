"""Explicit operator retry with the original external identity; never release uncertain funds."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from finance.models import RefundObligation
from core.models import OutboxEvent
from core.services import audit


class Command(BaseCommand):
    help = 'Requeue one pending/unknown refund using its existing provider idempotency key.'

    def add_arguments(self, parser):
        parser.add_argument('operation_id')

    @transaction.atomic
    def handle(self, *args, **options):
        operation = RefundObligation.objects.select_for_update().filter(pk=options['operation_id']).first()
        if not operation or operation.status not in ['pending', 'unknown', 'submitting']:
            raise CommandError('An unresolved refund operation is required.')
        if not operation.source_payment_id:
            raise CommandError('Legacy refund has no proven submission identity. Reconcile with the provider before remediation.')
        if operation.lease_until and operation.lease_until > timezone.now():
            raise CommandError('A worker still owns the refund lease.')
        operation.next_attempt_at = timezone.now()
        operation.save(update_fields=['next_attempt_at'])
        OutboxEvent.objects.filter(key=f'refund:{operation.pk}:requested').update(
            available_at=timezone.now(), processed_at=None, dead_lettered_at=None,
            attempts=0, last_error='', failure_owner='')
        audit(None, 'refund.operator_retry', operation.pk, operation.facility,
              {'attempts_so_far': operation.attempts})
        self.stdout.write('Refund requeued with the original operation identity; funds remain reserved.')
