"""Durable refund submission. Provider calls never run under application row locks."""
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from core.services import audit, enqueue
from .adapters import adapter
from .models import RefundObligation
from .services import lock_appointment, record_succeeded_payment, sync_payment_state

ACTIVE = ['pending', 'submitting', 'unknown']


def process_refund(operation_id):
    if settings.RESTORE_QUARANTINE:
        return 'quarantined'
    import logging
    logging.getLogger('smartcare.refund').info('refund', extra={'event': 'refund.attempt', 'operation_id': str(operation_id)})
    now = timezone.now()
    with transaction.atomic():
        operation = RefundObligation.objects.select_for_update().get(pk=operation_id)
        if operation.status not in ACTIVE:
            return operation.status
        if operation.lease_until and operation.lease_until > now:
            return operation.status
        if operation.next_attempt_at is None or operation.next_attempt_at > now:
            return operation.status
        operation.status = 'submitting'
        operation.attempts += 1
        operation.lease_until = now + timedelta(seconds=settings.REFUND_LEASE_SECONDS)
        operation.save(update_fields=['status', 'attempts', 'lease_until'])
        attempt = operation.attempts
    # Stable UUID was committed with both the reservation of funds and the outbox event.
    # The configured demo providers deduplicate this identity. Production has no adapter.
    try:
        if not operation.source_payment_id:
            raise ValueError('Legacy refund requires reconciliation')
        result = adapter().refund(key=f'refund:{operation.pk}',
            payment_reference=operation.source_payment.provider_reference, amount=operation.amount)
        status = result.status if result.status in ['succeeded', 'failed', 'pending'] else 'unknown'
        reference = result.reference
        if not isinstance(reference, str) or not 0 < len(reference) <= 64:
            status, reference = 'unknown', ''
    except Exception:
        # Do not log response bodies, secrets, or raw provider exception strings.
        status, reference = 'unknown', ''

    with transaction.atomic():
        appointment, _ = lock_appointment(operation.appointment_id)
        operation = RefundObligation.objects.select_for_update().get(pk=operation_id)
        if operation.status == 'refunded':
            return operation.status
        # A later lease owns this attempt; stale failure must never overwrite its result.
        if operation.attempts != attempt:
            return operation.status
        operation.lease_until = None
        operation.provider_reference = reference
        if status == 'succeeded':
            record_succeeded_payment(appointment=appointment, amount=operation.amount,
                method=operation.method, actor=operation.actor,
                event_id=f'refund:{operation.pk}', reference=reference,
                obligation=operation, kind='refund')
            operation.status = 'refunded'
            operation.next_attempt_at = None
            operation.failure_owner = operation.last_error = ''
            enqueue(f'refund:{operation.pk}:recorded', 'payment.refunded',
                    {'obligation_id': str(operation.pk)})
        else:
            operation.status = status
            operation.failure_owner = 'finance'
            operation.last_error = 'Provider outcome requires reconciliation.' if status != 'failed' else 'Provider confirmed refund failure.'
            operation.next_attempt_at = (
                now + timedelta(seconds=min(3600, 30 * 2 ** min(attempt, 7))))
            if status == 'failed' or attempt >= settings.REFUND_MAX_ATTEMPTS:
                operation.next_attempt_at = None
        operation.version += 1
        operation.save()
        sync_payment_state(appointment)
        audit(operation.actor, f'refund.{operation.status}', operation.pk, operation.facility,
              {'attempt': attempt})
        return operation.status
