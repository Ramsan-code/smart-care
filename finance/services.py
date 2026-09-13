import uuid
import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.db.models import Sum, Q
from django.utils import timezone
from accounts.permissions import facility_ids
from appointments.models import Appointment, AppointmentHistory, Reservation
from appointments.services import lock_slot, lock_slots, authorized_patient, owned_reservation, expire_locked
from core.services import audit, enqueue
from scheduling.models import Slot
from scheduling.services import BookingError, eligible
from .adapters import adapter, money
from .models import Checkout, Payment, RefundObligation, AppointmentChange, FinanceException, ExceptionNote


def money_str(value):
    return str(money(value))


def scoped_finance_appointments(user):
    from appointments.api import scoped_appointments
    return scoped_appointments(user)


def require_finance_staff(user, facility_id=None):
    if not user.is_authenticated or not user.is_active or user.role not in ['reception', 'finance', 'administrator']:
        raise BookingError('forbidden', 'Staff payment access is required.', 403)
    if not user.is_superuser and not user.memberships.filter(active=True, facility__active=True).exists():
        raise BookingError('forbidden', 'Staff payment access is required.', 403)
    if facility_id and int(facility_id) not in set(facility_ids(user)):
        raise BookingError('forbidden', 'This facility is outside your payment scope.', 403)


def require_exception_staff(user):
    if not user.is_authenticated or user.role not in ['finance', 'administrator']:
        raise BookingError('forbidden', 'Finance access is required.', 403)
    if user.role == 'finance' and not user.memberships.filter(active=True, facility__active=True).exists():
        raise BookingError('forbidden', 'Finance access is required.', 403)


def visible_payments(user):
    qs = Payment.objects.select_related('appointment', 'facility')
    if user.role == 'patient':
        return qs.filter(appointment__patient__user=user)
    if user.role in ['reception', 'finance', 'administrator']:
        return qs.filter(facility_id__in=facility_ids(user))
    return qs.none()


def visible_exceptions(user):
    require_exception_staff(user)
    qs = FinanceException.objects.select_related('appointment', 'facility', 'assigned_to')
    return qs if user.role == 'administrator' and user.is_superuser else qs.filter(Q(facility_id__in=facility_ids(user)) | Q(facility__isnull=True))


def net_paid(appointment):
    charges = Payment.objects.filter(appointment=appointment, kind='charge', status='succeeded').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    refunds = Payment.objects.filter(appointment=appointment, kind='refund', status='succeeded').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    return money(charges - refunds)


def funding_payments(appointment):
    """Follow immutable replacement links to the actual collected source payments."""
    ids = []
    current = appointment
    while current.pk not in ids:
        ids.append(current.pk)
        incoming = AppointmentChange.objects.filter(replacement=current, status='completed').select_related('original').first()
        if not incoming:
            break
        current = incoming.original
        if current.facility_id != appointment.facility_id:
            raise BookingError('validation', 'Cross-facility credit requires reconciliation.')
    return Payment.objects.filter(appointment_id__in=ids, facility_id=appointment.facility_id,
        currency=appointment.snapshot.get('currency', 'LKR'), kind='charge', status='succeeded')


def credited_amount(appointment):
    change = AppointmentChange.objects.filter(replacement=appointment, status='completed').first()
    return money(change.credited_amount) if change else money(0)


def appointment_financials(appointment):
    paid = net_paid(appointment)
    credit = credited_amount(appointment)
    total = money(appointment.snapshot['total'])
    covered = paid + credit
    due = max(money(0), total - covered)
    pending_refund = RefundObligation.objects.filter(appointment=appointment,
        status__in=['pending', 'submitting', 'unknown']).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    transferred = AppointmentChange.objects.filter(original=appointment, status='completed',
        kind='reschedule').aggregate(total=Sum('credited_amount'))['total'] or Decimal('0.00')
    refundable = max(money(0), paid + credit - pending_refund - transferred)
    return {'total': money_str(total), 'paid': money_str(paid), 'credited': money_str(credit),
            'due': money_str(due), 'refundable': money_str(refundable), 'pending_refund': money_str(pending_refund),
            'currency': appointment.snapshot.get('currency', 'LKR')}


def due_amount(appointment):
    return money(appointment_financials(appointment)['due'])


def payment_data(payment):
    return {'id': str(payment.pk), 'appointment_id': str(payment.appointment_id) if payment.appointment_id else None,
            'kind': payment.kind, 'method': payment.method, 'status': payment.status, 'amount': money_str(payment.amount),
            'currency': payment.currency, 'receipt_number': payment.receipt_number,
            'provider_event_id': payment.provider_event_id, 'provider_reference': payment.provider_reference,
            'created_at': payment.created_at.isoformat()}


def payment_history(user, appointment_id):
    appointment = accessible_appointment(user, appointment_id)
    return [payment_data(payment) for payment in Payment.objects.filter(
        appointment=appointment
    ).order_by("created_at", "pk")]


def checkout_data(checkout):
    return {'id': str(checkout.pk), 'appointment_id': str(checkout.appointment_id) if checkout.appointment_id else None,
            'change_id': str(checkout.change_id) if checkout.change_id else None, 'purpose': checkout.purpose,
            'amount': money_str(checkout.amount), 'currency': checkout.currency, 'status': checkout.status,
            'provider_reference': checkout.provider_reference, 'expires_at': checkout.expires_at.isoformat(),
            'version': checkout.version, 'checkout_url': f'/demo/checkout/{checkout.pk}/'}


def change_data(change):
    return {'id': str(change.pk), 'original_id': str(change.original_id),
            'replacement_id': str(change.replacement_id) if change.replacement_id else None,
            'hold_id': str(change.hold_id) if change.hold_id else None, 'kind': change.kind,
            'fee_direction': change.fee_direction, 'status': change.status, 'version': change.version,
            'credited_amount': money_str(change.credited_amount), 'additional_amount': money_str(change.additional_amount),
            'refund_amount': money_str(change.refund_amount)}


def exception_data(item):
    return {'id': str(item.pk), 'status': item.status, 'reason': item.reason, 'amount': money_str(item.amount),
            'currency': item.currency, 'provider_event_id': item.provider_event_id, 'provider_reference': item.provider_reference,
            'appointment_id': str(item.appointment_id) if item.appointment_id else None,
            'checkout_id': str(item.checkout_id) if item.checkout_id else None, 'version': item.version,
            'assigned_to': item.assigned_to_id, 'note': item.note, 'created_at': item.created_at.isoformat(),
            'history': [{'at': n.created_at.isoformat(), 'from': n.from_status, 'to': n.to_status, 'note': n.note}
                        for n in item.notes.order_by('created_at', 'pk')]}


def accessible_appointment(user, appointment_id):
    appointment = scoped_finance_appointments(user).filter(pk=appointment_id).select_related('patient', 'facility', 'reservation').first()
    if not appointment:
        raise BookingError('not_found', 'Appointment not found.', 404)
    if user.role == 'patient':
        authorized_patient(user, appointment.patient_id, appointment.facility_id)
    return appointment


def lock_appointment(appointment_id):
    hint = Appointment.objects.filter(pk=appointment_id).values('reservation__slot_id').first()
    if not hint:
        raise BookingError('not_found', 'Appointment not found.', 404)
    slot = lock_slot(hint['reservation__slot_id'])
    appointment = Appointment.objects.select_for_update().get(pk=appointment_id)
    return appointment, slot


def receipt_number():
    return 'RCPT-' + uuid.uuid4().hex[:12].upper()


def event_key():
    return 'pay_' + uuid.uuid4().hex


def sync_payment_state(appointment):
    financials = appointment_financials(appointment)
    if appointment.state == 'rescheduled':
        state = 'transferred' if money(financials['paid']) > 0 or money(financials['credited']) > 0 else appointment.payment_state
    elif money(financials['paid']) <= 0 and money(financials['credited']) <= 0:
        state = appointment.payment_state if appointment.payment_state in ['counter_due', 'hosted_pending'] else 'counter_due'
    elif money(financials['due']) <= 0 and money(financials['refundable']) <= 0 and money(financials['credited']) >= money(financials['total']):
        state = 'paid'
    elif money(financials['due']) <= 0:
        state = 'refunded' if money(financials['refundable']) <= 0 and money(financials['paid']) <= 0 else 'paid'
    elif money(financials['paid']) > 0 or money(financials['credited']) > 0:
        state = 'partially_refunded' if RefundObligation.objects.filter(appointment=appointment, status='refunded').exists() and money(financials['due']) > 0 else 'partially_refunded' if money(financials['refundable']) < net_paid(appointment) + money(financials['paid']) else 'hosted_pending' if appointment.payment_state == 'hosted_pending' else 'counter_due'
        if money(financials['due']) > 0 and money(financials['paid']) + money(financials['credited']) > 0:
            state = 'partially_refunded' if Payment.objects.filter(appointment=appointment, kind='refund', status='succeeded').exists() else appointment.payment_state
            if state not in ['counter_due', 'hosted_pending', 'partially_refunded']:
                state = 'counter_due'
        if money(financials['due']) <= 0:
            state = 'paid' if money(financials['paid']) > 0 or money(financials['credited']) > 0 else state
    else:
        state = appointment.payment_state
    if money(financials['due']) <= 0 and appointment.state in ['confirmed', 'rescheduled']:
        if appointment.state == 'rescheduled':
            state = 'transferred' if money(financials['paid']) > 0 else appointment.payment_state
        else:
            state = 'paid' if money(financials['paid']) + money(financials['credited']) >= money(financials['total']) else state
    if appointment.state == 'cancelled' and money(financials['refundable']) <= 0 and money(financials['paid']) <= 0 and Payment.objects.filter(appointment=appointment, kind='refund', status='succeeded').exists():
        state = 'refunded'
    if state != appointment.payment_state:
        appointment.payment_state = state
        appointment.version += 1
        appointment.save(update_fields=['payment_state', 'version'])
    return appointment


def set_state(appointment, user, to_state, source):
    from_state = appointment.state
    appointment.state = to_state
    appointment.version += 1
    appointment.save(update_fields=['state', 'version'])
    AppointmentHistory.objects.create(appointment=appointment, actor=user, source=source, from_state=from_state, to_state=to_state, version=appointment.version)


def open_checkout(user, appointment, amount, purpose, change=None):
    amount = money(amount)
    if amount <= 0:
        raise BookingError('validation', 'A positive amount is required for checkout.', 400)
    Checkout.objects.filter(appointment=appointment, purpose=purpose, status='pending').update(status='expired')
    if change:
        Checkout.objects.filter(change=change, status='pending').update(status='expired')
    checkout = Checkout(facility=appointment.facility, appointment=appointment if purpose == 'appointment' else change.replacement or appointment,
                        change=change, actor=user, purpose=purpose, amount=amount, currency=appointment.snapshot.get('currency', 'LKR'),
                        expires_at=timezone.now() + timedelta(minutes=15), provider_reference='pending')
    checkout.provider_reference = adapter().create_checkout(key=f'checkout:{uuid.uuid4().hex}', amount=amount, currency=checkout.currency).reference
    checkout.save()
    return checkout_data(checkout)


def create_checkout(user, appointment_id=None, change_id=None, expected_version=None):
    if bool(appointment_id) == bool(change_id):
        raise BookingError('validation', 'Supply either an appointment or a pending change.', 400)
    if change_id:
        change = AppointmentChange.objects.filter(pk=change_id).select_related('original').first()
        if not change:
            raise BookingError('not_found', 'Appointment change not found.', 404)
        accessible_appointment(user, change.original_id)
        appointment, _ = lock_appointment(change.original_id)
        change = AppointmentChange.objects.select_for_update().get(pk=change.pk)
        if change.version != expected_version:
            raise BookingError('stale_version', 'This change has been updated. Refresh before paying.')
        if change.status != 'pending_payment':
            raise BookingError('state_conflict', 'This change is not waiting for additional payment.')
        if user.role == 'doctor':
            raise BookingError('forbidden', 'Doctors cannot start payments.', 403)
        return open_checkout(user, appointment, change.additional_amount, 'reschedule_additional', change)
    appointment, _ = lock_appointment(appointment_id)
    accessible_appointment(user, appointment.pk)
    if expected_version is not None and appointment.version != expected_version:
        raise BookingError('stale_version', 'This appointment has changed. Refresh before paying.')
    if user.role == 'doctor':
        raise BookingError('forbidden', 'Doctors cannot start payments.', 403)
    due = due_amount(appointment)
    if appointment.state != 'confirmed' or due <= 0:
        raise BookingError('state_conflict', 'No balance remains for hosted payment.')
    return open_checkout(user, appointment, due, 'appointment')


def record_succeeded_payment(*, appointment, amount, method, actor, event_id, reference, checkout=None, obligation=None, kind='charge'):
    payment = Payment.objects.create(
        facility=appointment.facility if appointment else checkout.facility, appointment=appointment, checkout=checkout,
        obligation=obligation, actor=actor, kind=kind, method=method, status='succeeded', amount=money(amount),
        currency=(appointment.snapshot.get('currency') if appointment else checkout.currency),
        provider_event_id=event_id, provider_reference=reference,
        receipt_number=receipt_number() if kind == 'charge' else None,
        snapshot={'appointment': str(appointment.pk) if appointment else None, 'kind': kind})
    if appointment and kind == 'charge':
        sync_payment_state(appointment)
    elif appointment and kind == 'refund':
        sync_payment_state(appointment)
    return payment


def record_counter_payment(user, appointment_id, expected_version, amount, kind='charge'):
    require_finance_staff(user)
    appointment, _ = lock_appointment(appointment_id)
    accessible_appointment(user, appointment.pk)
    require_finance_staff(user, appointment.facility_id)
    if appointment.version != expected_version:
        raise BookingError('stale_version', 'This appointment has changed. Refresh before recording payment.')
    amount = money(amount)
    if amount <= 0:
        raise BookingError('validation', 'Enter a positive amount.', 400)
    if kind == 'charge':
        if appointment.state != 'confirmed':
            raise BookingError('state_conflict', 'Counter payment can only be recorded for a confirmed appointment.')
        due = due_amount(appointment)
        if amount > due:
            raise BookingError('validation', 'Amount exceeds the remaining balance.', 400)
        payment = record_succeeded_payment(appointment=appointment, amount=amount, method='counter', actor=user,
                                           event_id=event_key(), reference='counter-' + uuid.uuid4().hex[:12])
        audit(user, 'payment.recorded', payment.pk, appointment.facility, {'amount': money_str(amount), 'method': 'counter'})
        enqueue(f'payment:{payment.pk}:recorded', 'payment.recorded', {'payment_id': str(payment.pk)})
        from appointments.services import appointment_data
        return {'payment': payment_data(payment), 'appointment': appointment_data(appointment, user)}
    return execute_refund(user, appointment, amount, 'staff_partial', method='counter')


@transaction.atomic
def execute_refund(user, appointment, amount, reason, method='counter', change=None):
    # This is a durable intent only. A worker submits AFTER this transaction commits.
    appointment = Appointment.objects.select_for_update().get(pk=appointment.pk)
    amount = money(amount)
    refundable = money(appointment_financials(appointment)['refundable'])
    if amount <= 0 or amount > refundable:
        raise BookingError('validation', 'Refund amount must be within the unreserved collected balance.', 400)
    sources = list(funding_payments(appointment).select_for_update().order_by('pk'))
    operations = []
    remaining = amount
    for source in sources:
        reserved = RefundObligation.objects.filter(source_payment=source,
            status__in=['pending', 'submitting', 'unknown', 'refunded']).aggregate(total=Sum('amount'))['total'] or money(0)
        available = max(money(0), source.amount - reserved)
        allocation = min(remaining, available)
        if allocation <= 0:
            continue
        obligation = RefundObligation.objects.create(facility=appointment.facility, appointment=appointment, change=change,
            amount=allocation, currency=appointment.snapshot.get('currency', 'LKR'), reason=reason,
            actor=user, method=source.method, source_payment=source, next_attempt_at=timezone.now())
        operations.append(obligation)
        enqueue(f'refund:{obligation.pk}:requested', 'refund.requested', {'obligation_id': str(obligation.pk)})
        audit(user, 'refund.requested', obligation.pk, appointment.facility, {'amount': money_str(allocation)})
        remaining -= allocation
        if remaining == 0:
            break
    if remaining:
        raise BookingError('validation', 'Collected sources cannot cover this refund; finance reconciliation is required.')
    appointment.version += 1
    appointment.save(update_fields=['version'])
    from appointments.services import appointment_data
    return {'obligation_id': str(operations[0].pk), 'obligation_ids': [str(op.pk) for op in operations], 'status': 'pending',
            'appointment': appointment_data(appointment, user)}


@transaction.atomic
def record_refund(user, appointment_id, expected_version, amount, reason='staff_partial'):
    require_finance_staff(user)
    appointment, _ = lock_appointment(appointment_id)
    accessible_appointment(user, appointment.pk)
    require_finance_staff(user, appointment.facility_id)
    if appointment.version != expected_version:
        raise BookingError('stale_version', 'This appointment has changed. Refresh before recording a refund.')
    method = 'hosted' if appointment.payments.filter(kind='charge', method='hosted').exists() else 'counter'
    return execute_refund(user, appointment, amount, reason, method=method)


def raise_exception(*, reason, event_id, amount=0, currency='LKR', checkout=None, appointment=None, change=None, reference='', facility=None):
    existing = FinanceException.objects.filter(provider_event_id=event_id).first()
    if existing:
        return existing
    item, created = FinanceException.objects.get_or_create(provider_event_id=event_id, defaults=dict(
        facility=facility or (appointment.facility if appointment else None) or (checkout.facility if checkout else None),
        appointment=appointment, checkout=checkout, change=change, reason=reason, amount=money(amount), currency=currency,
        provider_reference=reference))
    if not created:
        return item
    if checkout and checkout.status == 'pending':
        checkout.status = 'unmatched'; checkout.version += 1
        checkout.save(update_fields=['status', 'version'])
    audit(None, 'finance.exception', item.pk, item.facility, {'reason': reason, 'event_id': event_id})
    enqueue(f'exception:{item.pk}:opened', 'payment.unmatched', {'exception_id': str(item.pk)})
    return item


def patient_can_change(user, appointment, now):
    if user.role in ['reception', 'administrator']:
        return True
    hours = int(appointment.snapshot.get('cancellation_hours', 24))
    return appointment.starts_at - now >= timedelta(hours=hours)


def release_capacity(slot, reservation):
    reservation.status = 'cancelled'; reservation.version += 1
    reservation.save(update_fields=['status', 'version'])
    if slot.active_reservation_id == reservation.pk:
        slot.active_reservation = None; slot.version += 1
        slot.save(update_fields=['active_reservation', 'version'])


def cancel_appointment(user, appointment_id, expected_version, reason='patient_request'):
    appointment, slot = lock_appointment(appointment_id)
    accessible_appointment(user, appointment.pk)
    if user.role == 'doctor':
        raise BookingError('forbidden', 'Doctors cannot cancel appointments.', 403)
    if appointment.version != expected_version:
        raise BookingError('stale_version', 'This appointment has changed. Refresh before cancelling.')
    if appointment.state != 'confirmed':
        raise BookingError('state_conflict', 'Only a confirmed appointment can be cancelled.')
    now = timezone.now()
    if not patient_can_change(user, appointment, now):
        raise BookingError('cutoff', 'Patient changes close 24 hours before the visit in this demo. Ask reception for help.', 409)
    paid = money(appointment_financials(appointment)['refundable'])
    source = 'patient' if user.role == 'patient' else user.role
    change = AppointmentChange.objects.create(facility=appointment.facility, original=appointment, actor=user, kind='cancellation',
                                              fee_direction='none', status='completed', refund_amount=paid)
    release_capacity(slot, appointment.reservation)
    set_state(appointment, user, 'cancelled', source)
    result = {'change': change_data(change)}
    if paid > 0:
        result.update(execute_refund(user if user.role != 'patient' else appointment.reservation.owner, appointment, paid, 'cancellation',
                                     method='hosted' if appointment.payments.filter(kind='charge', method='hosted').exists() else 'counter', change=change))
        appointment.refresh_from_db()
    audit(user, 'appointment.cancelled', appointment.pk, appointment.facility, {'reason': reason, 'refund': money_str(paid)})
    enqueue(f'appointment:{appointment.pk}:cancelled:{appointment.version}', 'appointment.cancelled', {'appointment_id': str(appointment.pk), 'version': appointment.version})
    from appointments.services import appointment_data
    result['appointment'] = appointment_data(appointment, user)
    return result


def preview_reschedule(original, hold):
    original_total = money(original.snapshot['total'])
    replacement_total = money(hold.snapshot['total'])
    if RefundObligation.objects.filter(appointment=original, status__in=['pending', 'submitting', 'unknown']).exists():
        raise BookingError('state_conflict', 'Resolve in-flight refunds before rescheduling.')
    paid = money(appointment_financials(original)['refundable'])
    credited = min(paid, replacement_total)
    additional = max(money(0), replacement_total - paid)
    refund = max(money(0), paid - replacement_total)
    if additional > 0:
        direction = 'higher'
    elif refund > 0:
        direction = 'lower'
    else:
        direction = 'same'
    return {'original_total': original_total, 'replacement_total': replacement_total, 'credited': credited,
            'additional': additional, 'refund': refund, 'direction': direction}


def commit_replacement(user, original, original_slot, hold, replacement_slot, preview, now):
    if hold.status != 'held' or hold.expires_at <= now or replacement_slot.active_reservation_id != hold.pk:
        raise BookingError('hold_expired', 'The replacement hold expired. The original appointment is unchanged.', 410)
    eligible(replacement_slot, now)
    source = 'patient' if user.role == 'patient' else user.role
    snapshot = {**hold.snapshot, 'consent_version': original.snapshot.get('consent_version'), 'consent_recorded_by': source,
                'credited_from': original.reference, 'credited_amount': money_str(preview['credited'])}
    if preview['replacement_total'] <= preview['credited']:
        payment_state = 'paid'
    else:
        payment_state = original.payment_state if original.payment_state in ['counter_due', 'hosted_pending'] else 'counter_due'
    replacement = Appointment.objects.create(reference='SC-' + uuid.uuid4().hex[:20].upper(), reservation=hold,
        patient=original.patient, facility=replacement_slot.session.facility, doctor=replacement_slot.session.doctor,
        service=replacement_slot.session.service, starts_at=replacement_slot.starts_at, ends_at=replacement_slot.ends_at,
        source=source, snapshot=snapshot, reason_category=original.reason_category, payment_state=payment_state)
    AppointmentHistory.objects.create(appointment=replacement, actor=user, source=source, from_state='held', to_state='confirmed', version=1)
    hold.status = 'booked'; hold.version += 1; hold.save(update_fields=['status', 'version'])
    replacement_slot.version += 1; replacement_slot.save(update_fields=['version'])
    release_capacity(original_slot, original.reservation)
    set_state(original, user, 'rescheduled', source)
    original.payment_state = 'transferred' if preview['credited'] > 0 else original.payment_state
    original.save(update_fields=['payment_state'])
    return replacement


def request_reschedule(user, appointment_id, hold_id, expected_version, expected_hold_version):
    original = accessible_appointment(user, appointment_id)
    if user.role == 'doctor':
        raise BookingError('forbidden', 'Doctors cannot reschedule appointments.', 403)
    hold = owned_reservation(user, hold_id)
    if str(hold.patient_id) != str(original.patient_id):
        raise BookingError('forbidden', 'The replacement hold belongs to a different patient.', 403)
    slots = lock_slots([original.reservation.slot_id, hold.slot_id])
    original_slot, replacement_slot = slots[0], slots[1]
    original = Appointment.objects.select_for_update().get(pk=original.pk)
    hold = Reservation.objects.select_for_update().get(pk=hold.pk)
    now = timezone.now()
    if original.version != expected_version:
        raise BookingError('stale_version', 'This appointment has changed. Refresh before rescheduling.')
    if hold.version != expected_hold_version:
        raise BookingError('stale_version', 'This hold has changed. Refresh before rescheduling.')
    if original.state != 'confirmed':
        raise BookingError('state_conflict', 'Only a confirmed appointment can be rescheduled.')
    if original.reservation.slot_id == hold.slot_id:
        raise BookingError('validation', 'Choose a different appointment time.', 400)
    if hold.slot.session.facility_id != original.facility_id:
        raise BookingError('forbidden', 'The replacement time must be in the same facility.', 403)
    if not patient_can_change(user, original, now):
        raise BookingError('cutoff', 'Patient changes close 24 hours before the visit in this demo. Ask reception for help.', 409)
    if AppointmentChange.objects.filter(original=original, status='pending_payment').exists():
        raise BookingError('state_conflict', 'A higher-fee change is already waiting for payment.')
    expire_locked(replacement_slot, now)
    hold = Reservation.objects.select_for_update().get(pk=hold.pk)
    preview = preview_reschedule(original, hold)
    source = 'patient' if user.role == 'patient' else user.role
    if preview['direction'] == 'higher':
        change = AppointmentChange.objects.create(facility=original.facility, original=original, hold=hold, actor=user,
            kind='reschedule', fee_direction='higher', status='pending_payment', credited_amount=preview['credited'],
            additional_amount=preview['additional'], refund_amount=0)
        checkout = open_checkout(user, original, preview['additional'], 'reschedule_additional', change)
        audit(user, 'change.pending', change.pk, original.facility, {'additional': money_str(preview['additional'])})
        from appointments.services import appointment_data
        return {'change': change_data(change), 'checkout': checkout, 'appointment': appointment_data(original, user),
                'next_action': f"Pay LKR {preview['additional']} to confirm the higher-fee move. The original appointment stays until payment succeeds."}
    replacement = commit_replacement(user, original, original_slot, hold, replacement_slot, preview, now)
    change = AppointmentChange.objects.create(facility=original.facility, original=original, replacement=replacement, hold=hold,
        actor=user, kind='reschedule', fee_direction=preview['direction'], status='completed',
        credited_amount=preview['credited'], additional_amount=0, refund_amount=preview['refund'])
    result = {'change': change_data(change)}
    if preview['refund'] > 0:
        result.update(execute_refund(user if user.role != 'patient' else original.reservation.owner, original, preview['refund'],
                                     'lower_fee_reschedule', method='hosted' if original.payments.filter(kind='charge', method='hosted').exists() else 'counter',
                                     change=change))
    audit(user, 'appointment.rescheduled', replacement.pk, replacement.facility,
          {'original': original.reference, 'direction': preview['direction']})
    enqueue(f'appointment:{replacement.pk}:rescheduled:1', 'appointment.rescheduled',
            {'appointment_id': str(replacement.pk), 'original_id': str(original.pk), 'replacement_id': str(replacement.pk), 'version': replacement.version})
    from appointments.services import appointment_data
    result['appointment'] = appointment_data(original, user)
    result['replacement'] = appointment_data(replacement, user)
    return result


def complete_pending_change(user, change, event_id, reference, method, amount, checkout=None):
    now = timezone.now()
    original_slot_id = Appointment.objects.filter(pk=change.original_id).values_list('reservation__slot_id', flat=True).first()
    hold_slot_id = Reservation.objects.filter(pk=change.hold_id).values_list('slot_id', flat=True).first()
    original_slot, replacement_slot = lock_slots([original_slot_id, hold_slot_id])
    original = Appointment.objects.select_for_update().get(pk=change.original_id)
    hold = Reservation.objects.select_for_update().get(pk=change.hold_id)
    change = AppointmentChange.objects.select_for_update().get(pk=change.pk)
    if change.status != 'pending_payment':
        raise BookingError('state_conflict', 'This change is not waiting for payment.')
    expire_locked(replacement_slot, now)
    hold = Reservation.objects.select_for_update().get(pk=change.hold_id)
    replacement_slot = Slot.objects.select_for_update().get(pk=replacement_slot.pk)
    if hold.status != 'held' or hold.expires_at <= now or replacement_slot.active_reservation_id != hold.pk:
        raise_exception(reason='late_payment', event_id=event_id, amount=amount, appointment=original,
                        change=change, reference=reference, facility=original.facility)
        change.status = 'failed'; change.version += 1
        change.save(update_fields=['status', 'version'])
        raise BookingError('late_payment', 'Replacement capacity is no longer available. The original appointment is unchanged.', 409)
    preview = preview_reschedule(original, hold)
    if money(amount) != preview['additional']:
        raise_exception(reason='amount_mismatch', event_id=event_id, amount=amount, appointment=original, change=change,
                        reference=reference, facility=original.facility)
        raise BookingError('amount_mismatch', 'Paid amount does not match the additional fee.', 409)
    replacement = commit_replacement(user, original, original_slot, hold, replacement_slot, preview, now)
    change.replacement = replacement; change.status = 'completed'; change.version += 1
    change.save(update_fields=['replacement', 'status', 'version'])
    actor = user if getattr(user, 'is_authenticated', False) else change.actor
    payment = record_succeeded_payment(appointment=replacement, amount=amount, method=method, actor=actor,
                                       event_id=event_id, reference=reference, checkout=checkout)
    replacement.refresh_from_db()
    sync_payment_state(replacement)
    audit(change.actor, 'appointment.rescheduled', replacement.pk, replacement.facility, {'original': original.reference, 'direction': 'higher'})
    enqueue(f'appointment:{replacement.pk}:rescheduled:1', 'appointment.rescheduled',
            {'appointment_id': str(replacement.pk), 'original_id': str(original.pk), 'replacement_id': str(replacement.pk), 'version': replacement.version})
    return payment, replacement


def record_change_payment(user, change_id, expected_version, amount):
    require_finance_staff(user)
    change = AppointmentChange.objects.filter(pk=change_id).first()
    if not change:
        raise BookingError('not_found', 'Appointment change not found.', 404)
    accessible_appointment(user, change.original_id)
    original_slot_id = Appointment.objects.filter(pk=change.original_id).values_list('reservation__slot_id', flat=True).get()
    hold_slot_id = Reservation.objects.filter(pk=change.hold_id).values_list('slot_id', flat=True).get()
    lock_slots([original_slot_id, hold_slot_id])
    change = AppointmentChange.objects.select_for_update().get(pk=change.pk)
    if change.version != expected_version:
        raise BookingError('stale_version', 'This change has been updated. Refresh before paying.')
    payment, replacement = complete_pending_change(user, change, event_key(), 'counter-' + uuid.uuid4().hex[:12], 'counter', amount)
    from appointments.services import appointment_data
    return {'payment': payment_data(payment), 'replacement': appointment_data(replacement, user), 'change': change_data(change)}


@transaction.atomic
def apply_callback_payload(payload, actor=None):
    event_id = payload['event_id']
    existing = Payment.objects.filter(provider_event_id=event_id).first()
    if existing:
        if (str(existing.checkout_id) != str(payload['checkout_id'])
                or existing.amount != money(payload['amount']) or existing.currency != payload['currency']
                or existing.provider_reference != payload['provider_reference'] or payload['status'] != 'succeeded'):
            raise BookingError('idempotency_conflict', 'Provider event identity was reused with different content.')
        return {'duplicate': True, 'payment': payment_data(existing)}
    existing_exc = FinanceException.objects.filter(provider_event_id=event_id).first()
    if existing_exc:
        return {'duplicate': True, 'exception': exception_data(existing_exc)}
    hint = Checkout.objects.filter(pk=payload['checkout_id']).first()
    if hint:
        if hint.change_id:
            change_hint = AppointmentChange.objects.get(pk=hint.change_id)
            slot_ids = [Appointment.objects.values_list('reservation__slot_id', flat=True).get(pk=change_hint.original_id),
                        Reservation.objects.values_list('slot_id', flat=True).get(pk=change_hint.hold_id)]
            lock_slots(slot_ids)
        else:
            lock_appointment(hint.appointment_id)
    checkout = Checkout.objects.select_for_update().filter(pk=payload['checkout_id']).first()
    if not checkout:
        item = raise_exception(reason='unmatched', event_id=event_id, amount=payload.get('amount') or 0,
                               reference=payload.get('provider_reference', ''))
        return {'exception': exception_data(item)}
    if payload['provider_reference'] != checkout.provider_reference:
        raise BookingError('invalid_reference', 'Provider reference does not match the checkout.', 400)
    existing = Payment.objects.filter(provider_event_id=event_id).first()
    if existing:
        if (str(existing.checkout_id) != str(payload['checkout_id'])
                or existing.amount != money(payload['amount']) or existing.currency != payload['currency']
                or existing.provider_reference != payload['provider_reference'] or payload['status'] != 'succeeded'):
            raise BookingError('idempotency_conflict', 'Provider event identity was reused with different content.')
        return {'duplicate': True, 'payment': payment_data(existing)}
    if checkout.status == 'succeeded':
        payment = Payment.objects.filter(checkout=checkout, kind='charge').first()
        return {'duplicate': True, 'checkout': checkout_data(checkout),
                'payment': payment_data(payment) if payment else None}
    if checkout.last_event_id == event_id:
        return {'duplicate': True, 'checkout': checkout_data(checkout)}
    if payload['currency'] != checkout.currency or money(payload['amount']) != money(checkout.amount):
        item = raise_exception(reason='amount_mismatch', event_id=event_id, amount=payload['amount'], checkout=checkout,
                               appointment=checkout.appointment, change=checkout.change, reference=payload['provider_reference'])
        return {'exception': exception_data(item)}
    checkout.last_event_id = event_id
    checkout.save(update_fields=['last_event_id'])
    if payload['status'] == 'failed':
        checkout.status = 'failed'; checkout.version += 1
        checkout.save(update_fields=['status', 'version'])
        audit(actor, 'payment.failed', checkout.pk, checkout.facility, {'event_id': event_id})
        return {'checkout': checkout_data(checkout)}
    if checkout.purpose == 'reschedule_additional':
        change = AppointmentChange.objects.select_for_update().get(pk=checkout.change_id)
        try:
            payment, replacement = complete_pending_change(change.actor, change, event_id, payload['provider_reference'], 'hosted', checkout.amount, checkout=checkout)
        except BookingError as exc:
            if exc.code in ['late_payment', 'amount_mismatch', 'hold_expired']:
                return {'exception': exception_data(FinanceException.objects.get(provider_event_id=event_id)), 'code': exc.code}
            raise
        checkout.status = 'succeeded'; checkout.version += 1
        checkout.appointment = replacement
        checkout.save(update_fields=['status', 'version', 'appointment'])
        return {'payment': payment_data(payment), 'duplicate': False}
    appointment, _ = lock_appointment(checkout.appointment_id)
    if appointment.state != 'confirmed' or due_amount(appointment) <= 0 or money(payload['amount']) != due_amount(appointment):
        reason = 'late_payment' if appointment.state != 'confirmed' else 'overpayment' if due_amount(appointment) <= 0 else 'amount_mismatch'
        item = raise_exception(reason=reason, event_id=event_id, amount=payload['amount'], checkout=checkout,
                               appointment=appointment, reference=payload['provider_reference'])
        return {'exception': exception_data(item)}
    payment = record_succeeded_payment(appointment=appointment, amount=checkout.amount, method='hosted', actor=actor or checkout.actor,
                                       event_id=event_id, reference=payload['provider_reference'], checkout=checkout)
    checkout.status = 'succeeded'; checkout.version += 1
    checkout.save(update_fields=['status', 'version'])
    audit(checkout.actor, 'payment.recorded', payment.pk, appointment.facility, {'method': 'hosted', 'event_id': event_id})
    enqueue(f'payment:{payment.pk}:recorded', 'payment.recorded', {'payment_id': str(payment.pk)})
    return {'payment': payment_data(payment), 'duplicate': False}


@transaction.atomic
def apply_signed_callback(body, signature):
    try:
        payload = adapter().verify_callback(body=body, signature=signature)
    except ValueError as exc:
        if str(exc) == 'invalid_signature':
            raise BookingError('invalid_signature', 'The payment callback signature is invalid.', 400) from exc
        event_id = 'malformed-' + hashlib.sha256(body).hexdigest()[:40]
        item = raise_exception(reason='malformed', event_id=event_id, reference='')
        return {'exception': exception_data(item)}
    return apply_callback_payload(payload)


def simulate_checkout(user, checkout_id, outcome):
    if not (settings.DEBUG and settings.DEMO_MODE):
        raise BookingError('not_found', 'Checkout simulation is unavailable.', 404)
    if not user.is_authenticated:
        raise BookingError('forbidden', 'Sign in to control the simulated payment.', 403)
    checkout = Checkout.objects.filter(pk=checkout_id).first()
    if not checkout:
        raise BookingError('not_found', 'Checkout not found.', 404)
    if checkout.appointment_id:
        accessible_appointment(user, checkout.appointment_id)
    elif checkout.change_id:
        accessible_appointment(user, checkout.change.original_id)
    if outcome not in ['success', 'failure', 'delay', 'duplicate']:
        raise BookingError('validation', 'Choose success, failure, delay or duplicate.', 400)
    if outcome == 'duplicate':
        if not checkout.last_event_id:
            raise BookingError('state_conflict', 'No previous callback exists to duplicate.')
        status = 'succeeded' if checkout.status in ['succeeded', 'unmatched'] else 'failed' if checkout.status == 'failed' else 'succeeded'
        body, signature, payload = adapter().callback_body(checkout, status, checkout.last_event_id)
        result = apply_signed_callback(body, signature)
        result['simulated'] = True
        return result
    status = 'succeeded' if outcome in ['success', 'delay'] else 'failed'
    body, signature, payload = adapter().callback_body(checkout, status)
    checkout.last_event_id = payload['event_id']
    checkout.save(update_fields=['last_event_id'])
    if outcome == 'delay':
        enqueue(f'payment-callback:{payload["event_id"]}', 'payment.callback.delayed',
                {'body': body.decode(), 'signature': signature, 'event_id': payload['event_id']})
        event = __import__('core.models', fromlist=['OutboxEvent']).OutboxEvent.objects.get(key=f'payment-callback:{payload["event_id"]}')
        event.available_at = timezone.now() + timedelta(seconds=10)
        event.save(update_fields=['available_at'])
        return {'delayed': True, 'event_id': payload['event_id'], 'available_at': event.available_at.isoformat()}
    result = apply_signed_callback(body, signature)
    result['simulated'] = True
    return result


def assign_exception(user, exception_id, expected_version, note=''):
    require_exception_staff(user)
    item = visible_exceptions(user).filter(pk=exception_id).first()
    if not item:
        raise BookingError('not_found', 'Finance exception not found.', 404)
    item = FinanceException.objects.select_for_update().get(pk=item.pk)
    if item.version != expected_version:
        raise BookingError('stale_version', 'This exception has changed. Refresh before continuing.')
    if item.status == 'resolved':
        raise BookingError('state_conflict', 'A resolved exception cannot be reassigned.')
    from_status = item.status
    item.status = 'assigned'; item.assigned_to = user; item.note = note[:240]; item.version += 1
    item.save(update_fields=['status', 'assigned_to', 'note', 'version'])
    ExceptionNote.objects.create(exception=item, actor=user, from_status=from_status, to_status='assigned', note=note[:240] or 'Assigned')
    audit(user, 'exception.assigned', item.pk, item.facility)
    return exception_data(item)


def resolve_exception(user, exception_id, expected_version, note):
    require_exception_staff(user)
    if not note:
        raise BookingError('validation', 'Record a resolution reason.', 400)
    item = visible_exceptions(user).filter(pk=exception_id).first()
    if not item:
        raise BookingError('not_found', 'Finance exception not found.', 404)
    item = FinanceException.objects.select_for_update().get(pk=item.pk)
    if item.version != expected_version:
        raise BookingError('stale_version', 'This exception has changed. Refresh before continuing.')
    if item.status == 'resolved':
        return exception_data(item)
    from_status = item.status
    item.status = 'resolved'; item.note = note[:240]; item.resolved_at = timezone.now(); item.version += 1
    if not item.assigned_to_id:
        item.assigned_to = user
    item.save(update_fields=['status', 'note', 'resolved_at', 'version', 'assigned_to'])
    ExceptionNote.objects.create(exception=item, actor=user, from_status=from_status, to_status='resolved', note=note[:240])
    audit(user, 'exception.resolved', item.pk, item.facility, {'note': note[:240]})
    return exception_data(item)
