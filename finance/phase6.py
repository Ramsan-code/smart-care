"""Phase 6 finance workflows.

Simulated transaction CSV columns: ``transaction_reference,amount,status,provider_event_id``.
Rows are matched to a payment by transaction reference, amount and succeeded status.
"""
import csv
import hashlib
import io
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponse
from django.utils import timezone

from accounts.permissions import facility_ids
from appointments.models import Appointment
from configuration.models import FeeVersion
from configuration.models import Facility
from core.services import audit
from scheduling.services import BookingError
from .models import (DoctorPayable, ExceptionNote, FinanceException, GatewayImport,
                     GatewayImportRow, Payment, SettlementBatch, SettlementLine, SettlementMutex)
from .services import require_exception_staff, require_finance_staff, raise_exception, funding_payments


def _facility(user, facility_id):
    require_finance_staff(user, facility_id)
    if user.role == 'administrator':
        return int(facility_id)
    if int(facility_id) not in set(facility_ids(user)):
        raise BookingError('forbidden', 'This facility is outside your scope.', 403)
    return int(facility_id)


def _settlement_staff(user, facility_id=None, require_approver=False):
    if facility_id:
        require_finance_staff(user, facility_id)
    if user.role not in ['finance', 'administrator']:
        raise BookingError('forbidden', 'Finance settlement access is required.', 403)
    if require_approver and facility_id and user.role != 'administrator' and not user.memberships.filter(
            facility_id=facility_id, active=True, finance_approver=True).exists():
        raise BookingError('forbidden', 'A finance approver is required.', 403)
    return facility_id


def lock_settlements(facility_id):
    mutex, _ = SettlementMutex.objects.get_or_create(facility_id=facility_id)
    SettlementMutex.objects.select_for_update().get(pk=mutex.pk)


@transaction.atomic
def import_gateway_csv(user, facility_id, uploaded):
    facility_id = _facility(user, facility_id)
    data = uploaded.read()
    digest = hashlib.sha256(data).hexdigest()
    batch, created = GatewayImport.objects.get_or_create(
        facility_id=facility_id, file_hash=digest,
        defaults={'uploaded_by': user, 'filename': uploaded.name, 'status': 'pending'})
    if not created:
        return gateway_import_data(batch)
    try:
        text = data.decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(text))
        required = {'transaction_reference', 'amount', 'status', 'provider_event_id'}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError('CSV must contain transaction_reference, amount, status, provider_event_id.')
        for number, raw in enumerate(reader, 2):
            reference = (raw.get('transaction_reference') or '').strip()
            event_id = (raw.get('provider_event_id') or '').strip()
            try:
                amount = Decimal((raw.get('amount') or '').strip()).quantize(Decimal('0.01'))
            except (InvalidOperation, ValueError):
                amount = None
            status = (raw.get('status') or '').strip().lower()
            payment = Payment.objects.filter(facility_id=facility_id, provider_reference=reference,
                                              status='succeeded', kind='charge').first()
            row_status, error = 'matched', ''
            if not reference or not event_id or amount is None:
                row_status, error = 'invalid', 'Missing reference/event or invalid amount.'
            elif not payment:
                row_status, error = 'unmatched', 'No payment matched transaction reference.'
            elif payment.amount != amount or status not in ('succeeded', 'paid', 'success'):
                row_status, error = 'amount_mismatch', 'Amount or gateway status does not match payment.'
            row = GatewayImportRow.objects.create(
                batch=batch, row_number=number, transaction_reference=reference, amount=amount,
                status_value=status, provider_event_id=event_id, row_status=row_status, error=error,
                payment=payment, raw=raw)
            if row_status in ('unmatched', 'amount_mismatch', 'invalid'):
                reason = 'amount_mismatch' if row_status == 'amount_mismatch' else 'unmatched'
                raise_exception(reason=reason, event_id=('csv-' + hashlib.sha256(
                    f'{digest}:{number}'.encode()).hexdigest()[:56]), amount=amount or 0,
                    reference=reference, facility=payment.facility if payment else Facility.objects.get(pk=facility_id))
        batch.status = 'processed'
        batch.save(update_fields=['status'])
    except (UnicodeDecodeError, csv.Error, ValueError):
        batch.status = 'failed'
        batch.save(update_fields=['status'])
        raise
    audit(user, 'gateway.csv_imported', batch.pk, batch.facility, {'rows': batch.rows.count()})
    return gateway_import_data(batch)


def gateway_import_data(batch):
    return {'id': batch.pk, 'filename': batch.filename, 'status': batch.status,
            'rows': [{'row_number': r.row_number, 'status': r.row_status, 'error': r.error,
                      'reference': r.transaction_reference} for r in batch.rows.order_by('row_number')]}


@transaction.atomic
def create_payables(user, facility_id):
    facility_id = _facility(user, facility_id)
    lock_settlements(facility_id)
    appointments = Appointment.objects.filter(facility_id=facility_id, state='completed',
                                                payment_state='paid').select_related('doctor', 'service')
    created = []
    for appointment in appointments:
        if FinanceException.objects.filter(appointment=appointment).exclude(status='resolved').exists():
            continue
        payment = funding_payments(appointment).order_by('created_at', 'pk').first()
        if not payment or DoctorPayable.objects.filter(appointment=appointment).exists():
            continue
        amount = appointment.snapshot.get('doctor_fee')
        if amount is None:
            raise BookingError('validation', 'Stored doctor fee breakdown is required; reconcile this appointment before creating a payable.')
        created.append(DoctorPayable.objects.create(
            facility_id=facility_id, doctor=appointment.doctor, appointment=appointment,
            source_payment=payment, amount=Decimal(amount), currency=payment.currency))
    audit(user, 'doctor_payables.versioned', facility_id, None, {'count': len(created)})
    return [{'id': p.pk, 'doctor_id': p.doctor_id, 'appointment_id': p.appointment_id,
             'amount': str(p.amount), 'currency': p.currency, 'status': p.status} for p in created]


def doctor_statement(user, doctor_id=None):
    if user.role != 'doctor':
        raise BookingError('forbidden', 'Only doctors may view doctor statements.', 403)
    qs = DoctorPayable.objects.filter(doctor__user=user).select_related('appointment', 'facility')
    if doctor_id and not qs.filter(doctor_id=doctor_id).exists():
        raise BookingError('forbidden', 'This statement is outside your scope.', 403)
    return [{'id': p.pk, 'appointment_id': str(p.appointment_id), 'amount': str(p.amount),
             'currency': p.currency, 'status': p.status, 'created_at': p.created_at.isoformat()} for p in qs.order_by('-created_at')]


@transaction.atomic
def create_settlement(user, facility_id, reference, adjustment=0, currency='LKR'):
    facility_id = _settlement_staff(user, facility_id)
    # Serializes settlement creation, adjustments and transitions within a facility.
    lock_settlements(facility_id)
    value = Decimal(adjustment)
    if value != 0:
        raise BookingError('validation', 'Use a linked refund adjustment instead of an unbacked batch adjustment.')
    existing = SettlementBatch.objects.filter(facility_id=facility_id, reference=reference).first()
    if existing:
        if existing.created_by_id != user.pk or existing.currency != currency or existing.adjustment != value:
            raise BookingError('idempotency_conflict', 'Settlement reference already used with different input.')
        return settlement_data(existing)
    payables = list(DoctorPayable.objects.select_for_update().filter(
        facility_id=facility_id, currency=currency, status='versioned',
        original_allocation__isnull=True).order_by('pk'))
    batch = SettlementBatch.objects.create(facility_id=facility_id, reference=reference,
        currency=currency, adjustment=value, created_by=user)
    for payable in payables:
        if payable.source_payment.currency != currency or payable.source_payment.facility_id != facility_id:
            raise BookingError('validation', 'Payable source payment has a different facility or currency.')
        SettlementLine.objects.create(batch=batch, payable=payable, original_payable=payable,
                                      doctor=payable.doctor, amount=payable.amount)
    audit(user, 'settlement.created', batch.pk, batch.facility, {'currency': currency})
    return settlement_data(batch)


@transaction.atomic
def add_refund_adjustment(user, paid_batch_id, payable_id, amount, refund_id):
    hint = DoctorPayable.objects.filter(pk=payable_id).first()
    if not hint:
        raise BookingError('not_found', 'Payable not found.', 404)
    _settlement_staff(user, hint.facility_id)
    lock_settlements(hint.facility_id)
    source = DoctorPayable.objects.select_for_update().get(pk=payable_id)
    original = SettlementLine.objects.filter(batch_id=paid_batch_id,
        batch__status='paid', batch__facility_id=source.facility_id,
        original_payable=source, kind='original').select_related('batch').first()
    if not original:
        raise BookingError('validation', 'Payable must belong to the specified paid settlement.')
    refund = Payment.objects.filter(pk=refund_id, appointment_id=source.appointment_id,
        facility_id=source.facility_id, currency=source.currency, kind='refund', status='succeeded').first()
    if not refund or original.batch.currency != source.currency:
        raise BookingError('validation', 'A successful refund in the same appointment, facility and currency is required.')
    value = Decimal(amount)
    prior = SettlementLine.objects.filter(refund=refund).first()
    if prior:
        if prior.original_line_id != original.pk or prior.payable_id != source.pk or prior.amount != -value:
            raise BookingError('idempotency_conflict', 'Refund already allocated with different input.')
        return settlement_data(prior.batch)
    # Pro rata doctor share, rounded down per refund; never silently assign the whole refund to the doctor.
    snapshot = source.appointment.snapshot
    try:
        total = Decimal(snapshot['total'])
        doctor_fee = Decimal(snapshot['doctor_fee'])
    except (KeyError, InvalidOperation, TypeError):
        raise BookingError('validation', 'Immutable fee breakdown is required for a refund adjustment.')
    if not total.is_finite() or not doctor_fee.is_finite() or not 0 < doctor_fee <= total or source.amount > doctor_fee:
        raise BookingError('validation', 'Invalid payable fee breakdown; finance review is required.')
    eligible = (refund.amount * source.amount / total).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
    allocated = -(SettlementLine.objects.filter(payable=source, kind='refund').aggregate(
        total=Sum('amount'))['total'] or Decimal('0'))
    if not value.is_finite() or value <= 0 or value > eligible or allocated + value > source.amount:
        raise BookingError('validation', 'Adjustment exceeds the eligible doctor share or remaining payable balance.')
    batch = SettlementBatch.objects.create(
        facility_id=source.facility_id, reference=f'REFUND-{refund.pk}',
        currency=source.currency, created_by=user)
    SettlementLine.objects.create(batch=batch, payable=source, doctor=source.doctor,
        kind='refund', original_line=original, refund=refund,
        amount=-value, description=f'Refund adjustment for settlement {original.batch_id}')
    audit(user, 'settlement.refund_adjustment', batch.pk, batch.facility,
          {'original_batch': original.batch_id, 'payable': source.pk, 'refund': str(refund.pk), 'amount': str(value)})
    return settlement_data(batch)


def settlement_data(batch):
    total = sum((line.amount + line.adjustment for line in batch.lines.all()), Decimal('0'))
    total += batch.adjustment
    return {'id': batch.pk, 'reference': batch.reference, 'status': batch.status,
            'currency': batch.currency, 'adjustment': format(batch.adjustment, '.2f'), 'total': format(total, '.2f'), 'lines': batch.lines.count()}


@transaction.atomic
def change_settlement(user, batch_id, action):
    hint = SettlementBatch.objects.filter(pk=batch_id).first()
    if not hint:
        raise BookingError('not_found', 'Settlement not found.', 404)
    _settlement_staff(user, hint.facility_id)
    lock_settlements(hint.facility_id)
    batch = SettlementBatch.objects.select_for_update().get(pk=batch_id)
    _settlement_staff(user, batch.facility_id, require_approver=(action == 'approve'))
    if action == 'review' and batch.status == 'draft':
        batch.status = 'review'
    elif action == 'approve':
        if batch.created_by_id == user.pk:
            raise BookingError('forbidden', 'A different finance approver must approve this settlement.', 403)
        if user.role != 'administrator' and not user.memberships.filter(
                facility=batch.facility, active=True, finance_approver=True).exists():
            raise BookingError('forbidden', 'A finance approver is required.', 403)
        if batch.status != 'review': raise BookingError('state_conflict', 'Batch must be under review.')
        batch.status, batch.approved_by = 'approved', user
    elif action == 'paid':
        if batch.status != 'approved': raise BookingError('state_conflict', 'Batch must be approved.')
        batch.status, batch.paid_at = 'paid', timezone.now()
        for line in batch.lines.filter(kind='original').select_related('payable'):
            line.payable.status, line.payable.paid_at = 'paid', batch.paid_at
            line.payable.save(update_fields=['status', 'paid_at'])
    else: raise BookingError('validation', 'Unsupported settlement action.')
    batch.save(update_fields=['status', 'approved_by', 'paid_at'])
    audit(user, f'settlement.{action}', batch.pk, batch.facility, {})
    return settlement_data(batch)


def export_settlement(user, batch_id):
    batch = SettlementBatch.objects.get(pk=batch_id)
    _settlement_staff(user, batch.facility_id)
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="settlement-{batch.reference}.csv"'
    writer = csv.writer(response)
    writer.writerow(['doctor_id', 'appointment_id', 'amount', 'adjustment', 'currency'])
    for line in batch.lines.select_related('payable'):
        writer.writerow([line.doctor_id, line.payable.appointment_id if line.payable else '',
                         line.amount, line.adjustment, batch.currency])
    if batch.adjustment:
        writer.writerow(['', '', '0.00', batch.adjustment, batch.currency])
    return response


def operations_report(user, facility_id):
    facility_id = _facility(user, facility_id)
    return {
        'facility_id': facility_id,
        'open_exceptions': FinanceException.objects.filter(facility_id=facility_id).exclude(status='resolved').count(),
        'unmatched_import_rows': GatewayImportRow.objects.filter(batch__facility_id=facility_id).exclude(row_status='matched').count(),
        'versioned_payables': str(sum((p.amount for p in DoctorPayable.objects.filter(
            facility_id=facility_id, status='versioned')), Decimal('0'))),
        'paid_payables': str(sum((p.amount for p in DoctorPayable.objects.filter(
            facility_id=facility_id, status='paid')), Decimal('0'))),
        'settlement_batches': SettlementBatch.objects.filter(facility_id=facility_id).count(),
    }
