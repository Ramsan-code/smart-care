"""Phase 6 finance workflows.

Simulated transaction CSV columns: ``transaction_reference,amount,status,provider_event_id``.
Rows are matched to a payment by transaction reference, amount and succeeded status.
"""
import csv
import hashlib
import io
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone

from accounts.permissions import facility_ids
from appointments.models import Appointment
from configuration.models import FeeVersion
from configuration.models import Facility
from core.services import audit
from scheduling.services import BookingError
from .models import (DoctorPayable, ExceptionNote, FinanceException, GatewayImport,
                     GatewayImportRow, Payment, SettlementBatch, SettlementLine)
from .services import require_exception_staff, require_finance_staff, raise_exception


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
    appointments = Appointment.objects.filter(facility_id=facility_id, state='completed',
                                                payment_state='paid').select_related('doctor', 'service')
    created = []
    for appointment in appointments:
        if FinanceException.objects.filter(appointment=appointment).exclude(status='resolved').exists():
            continue
        payment = Payment.objects.filter(appointment=appointment, kind='charge', status='succeeded').order_by('created_at').first()
        if not payment or DoctorPayable.objects.filter(appointment=appointment).exists():
            continue
        amount = appointment.snapshot.get('doctor_fee')
        if amount is None:
            fee = FeeVersion.objects.filter(facility_id=facility_id, service_id=appointment.service_id,
                                             effective_from__lte=appointment.starts_at.date()).order_by('-effective_from').first()
            amount = fee.doctor_fee if fee else 0
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
def create_settlement(user, facility_id, reference, adjustment=0):
    facility_id = _settlement_staff(user, facility_id)
    batch = SettlementBatch.objects.create(facility_id=facility_id, reference=reference,
                                            adjustment=Decimal(adjustment), created_by=user)
    for payable in DoctorPayable.objects.filter(facility_id=facility_id, status='versioned').exclude(
            settlement_lines__isnull=False):
        SettlementLine.objects.create(batch=batch, payable=payable, doctor=payable.doctor, amount=payable.amount)
    audit(user, 'settlement.created', batch.pk, batch.facility, {'adjustment': str(batch.adjustment)})
    return settlement_data(batch)


@transaction.atomic
def add_refund_adjustment(user, paid_batch_id, payable_id, amount):
    """Create a new negative line; the original paid batch is never changed."""
    source = DoctorPayable.objects.select_related('facility', 'doctor').get(pk=payable_id)
    old = SettlementBatch.objects.get(pk=paid_batch_id, status='paid', facility=source.facility)
    require_finance_staff(user, source.facility_id)
    value = Decimal(amount)
    if value <= 0 or value > source.amount:
        raise BookingError('validation', 'Refund adjustment must be positive and within payable amount.')
    batch = SettlementBatch.objects.create(
        facility=source.facility, reference=f'REFUND-{old.reference}-{source.pk}',
        currency=source.currency, created_by=user)
    SettlementLine.objects.create(batch=batch, payable=source, doctor=source.doctor,
                                  amount=-value, description=f'Refund after settlement {old.reference}')
    audit(user, 'settlement.refund_adjustment', batch.pk, batch.facility,
          {'original_batch': old.pk, 'payable': source.pk, 'amount': str(value)})
    return settlement_data(batch)


def settlement_data(batch):
    total = sum((line.amount + line.adjustment for line in batch.lines.all()), Decimal('0'))
    total += batch.adjustment
    return {'id': batch.pk, 'reference': batch.reference, 'status': batch.status,
            'adjustment': str(batch.adjustment), 'total': str(total), 'lines': batch.lines.count()}


@transaction.atomic
def change_settlement(user, batch_id, action):
    batch = SettlementBatch.objects.select_for_update().select_related('facility').get(pk=batch_id)
    _settlement_staff(user, batch.facility_id, require_approver=(action == 'approve'))
    if action == 'review' and batch.status == 'draft':
        batch.status = 'review'
    elif action == 'approve':
        if user.role != 'administrator' and not user.memberships.filter(
                facility=batch.facility, active=True, finance_approver=True).exists():
            raise BookingError('forbidden', 'A finance approver is required.', 403)
        if batch.status != 'review': raise BookingError('state_conflict', 'Batch must be under review.')
        batch.status, batch.approved_by = 'approved', user
    elif action == 'paid':
        if batch.status != 'approved': raise BookingError('state_conflict', 'Batch must be approved.')
        batch.status, batch.paid_at = 'paid', timezone.now()
        for line in batch.lines.select_related('payable'):
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
