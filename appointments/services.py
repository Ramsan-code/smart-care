import time
import uuid
from datetime import timedelta
from django.db import transaction, OperationalError
from django.utils import timezone
from accounts.models import Patient, Consent
from accounts.permissions import facility_ids
from configuration.models import Doctor
from core.services import audit, enqueue, execute_once
from scheduling.models import Session, Slot
from scheduling.services import BookingError, eligible
from decimal import Decimal
from .models import Reservation, Appointment, AppointmentHistory


def money_due(financials):
    return Decimal(str(financials.get('due', '0')))


def money_refundable(financials):
    return Decimal(str(financials.get('refundable', '0')))


def authorized_patient(user, patient_id=None, facility_id=None):
    if not user.is_authenticated or not user.is_active:
        raise BookingError('forbidden', 'Sign in to book an appointment.', 403)
    if user.role == 'patient':
        if not user.email_verified:
            raise BookingError('verification_required', 'Verify your email before booking.', 403)
        qs = Patient.objects.filter(user=user)
        if patient_id: qs = qs.filter(pk=patient_id)
    elif user.role == 'reception':
        qs = Patient.objects.filter(pk=patient_id, facility_id__in=facility_ids(user))
    else:
        raise BookingError('forbidden', 'Only patients and reception can book appointments.', 403)
    if facility_id: qs = qs.filter(facility_id=facility_id)
    patient = qs.first()
    if not patient: raise BookingError('not_found', 'Patient not found in your booking scope.', 404)
    return patient


def lock_slots(slot_ids):
    wanted = list(dict.fromkeys(slot_ids))
    hints = list(Slot.objects.filter(pk__in=wanted).values('pk', 'session_id', 'session__doctor_id'))
    if len(hints) != len(wanted):
        raise BookingError('not_found', 'Appointment time not found.', 404)
    for doctor_id in sorted({h['session__doctor_id'] for h in hints}):
        Doctor.objects.select_for_update().get(pk=doctor_id)
    for session_id in sorted({h['session_id'] for h in hints}):
        Session.objects.select_for_update().get(pk=session_id)
    locked = {s.pk: s for s in Slot.objects.select_for_update().select_related(
        'session__facility', 'session__doctor', 'session__service').filter(pk__in=wanted)}
    return [locked[slot_id] for slot_id in slot_ids]


def lock_slot(slot_id):
    return lock_slots([slot_id])[0]


def expire_locked(slot, now):
    if not slot.active_reservation_id: return
    reservation = Reservation.objects.select_for_update().get(pk=slot.active_reservation_id)
    if reservation.status == 'held' and reservation.expires_at <= now:
        reservation.status = 'expired'; reservation.version += 1
        reservation.save(update_fields=['status', 'version'])
        slot.active_reservation = None; slot.version += 1
        slot.save(update_fields=['active_reservation', 'version'])
        fail_pending_change(reservation, 'expired')
        audit(None, 'hold.expired', reservation.pk, slot.session.facility)


def reservation_data(reservation):
    return {'id': str(reservation.pk), 'slot_id': str(reservation.slot_id), 'patient_id': str(reservation.patient_id),
            'expires_at': reservation.expires_at.isoformat(), 'status': reservation.status, 'version': reservation.version,
            'fee_preview': reservation.snapshot, 'policy_version': reservation.snapshot['policy_version']}


def fail_pending_change(reservation, reason):
    from finance.models import AppointmentChange
    for change in AppointmentChange.objects.select_for_update().filter(hold=reservation, status='pending_payment'):
        change.status = 'failed'; change.version += 1
        change.save(update_fields=['status', 'version'])
        audit(None, 'change.failed', change.pk, change.facility, {'reason': reason, 'original_id': str(change.original_id)})


def appointment_data(appointment, user=None):
    from finance.services import appointment_financials
    financials = appointment_financials(appointment)
    if getattr(user, 'role', None) == 'doctor':
        financials = {'total': financials['total'], 'currency': financials['currency']}
    actions, next_action = allowed_actions(appointment, user, financials)
    return {'id': str(appointment.pk), 'reference': appointment.reference, 'patient_id': str(appointment.patient_id),
            'patient_name': appointment.patient.name, 'starts_at': appointment.starts_at.isoformat(), 'ends_at': appointment.ends_at.isoformat(),
            'state': appointment.state, 'payment_state': appointment.payment_state, 'source': appointment.source,
            'version': appointment.version, 'snapshot': appointment.snapshot, 'reason_category': appointment.reason_category,
            'financials': financials, 'allowed_actions': actions, 'next_action': next_action}


def allowed_actions(appointment, user, financials):
    actions = []
    if appointment.state != 'confirmed':
        return actions, 'This appointment is no longer active.'
    role = getattr(user, 'role', None)
    if role == 'doctor':
        return actions, 'Consultation outcomes arrive in Phase 5.'
    due = money_due(financials)
    refundable = money_refundable(financials)
    if due > 0 and role in ['patient', 'reception', 'finance', 'administrator']:
        actions.append('pay_hosted')
    if due > 0 and role in ['reception', 'finance']:
        actions.append('pay_counter')
    if refundable > 0 and role in ['reception', 'finance']:
        actions.append('refund')
    if role in ['patient', 'reception', 'administrator']:
        actions.extend(['cancel', 'reschedule'])
    if 'pay_hosted' in actions or 'pay_counter' in actions:
        next_action = f"LKR {financials.get('due', '0.00')} remains due. Pay at the counter or through the simulated hosted checkout."
    elif appointment.payment_state == 'paid':
        next_action = 'Payment is complete. Cancel or reschedule according to the 24-hour demo cutoff.'
    else:
        next_action = 'Review this confirmation, then cancel or reschedule if needed.'
    return actions, next_action


def command(user, operation, key, payload, action):
    if not key or len(key) > 100:
        raise BookingError('idempotency_required', 'Supply an Idempotency-Key of 1–100 characters.', 400)
    # Retry the complete database command, preserving its identity. No external side effects occur here.
    from django.core.exceptions import ValidationError
    for attempt in range(3):
        try:
            return execute_once(user, operation, key, payload, action)
        except ValidationError as exc:
            raise BookingError('idempotency_conflict', '; '.join(exc.messages)) from exc
        except OperationalError as exc:
            if exc.args[0] not in (1205, 1213) or attempt == 2:
                raise BookingError('retryable', 'Database busy; retry using the same request key.', 503) from exc
            time.sleep(.03 * (attempt + 1))


@transaction.atomic
def hold(user, slot_id, expected_version, patient_id=None):
    slot = lock_slot(slot_id)
    patient = authorized_patient(user, patient_id, slot.session.facility_id)
    now = timezone.now()
    snapshot = eligible(slot, now)
    if slot.version != expected_version:
        raise BookingError('stale_version', 'Availability changed. Refresh the appointment times.')
    expire_locked(slot, now)
    if slot.active_reservation_id:
        raise BookingError('capacity_conflict', 'Another person has reserved this time. Choose another slot.')
    reservation = Reservation.objects.create(slot=slot, owner=user, patient=patient, snapshot=snapshot,
                                            expires_at=min(now + timedelta(minutes=snapshot['hold_minutes']), slot.starts_at))
    slot.active_reservation = reservation; slot.version += 1
    slot.save(update_fields=['active_reservation', 'version'])
    audit(user, 'hold.created', reservation.pk, slot.session.facility, {'slot_id': str(slot.pk)})
    return reservation_data(reservation)


def owned_reservation(user, reservation_id):
    reservation = Reservation.objects.filter(pk=reservation_id, owner=user).first()
    if not reservation: raise BookingError('not_found', 'Hold not found.', 404)
    authorized_patient(user, reservation.patient_id, reservation.slot.session.facility_id)
    return reservation


@transaction.atomic
def release(user, reservation_id, expected_version):
    hint = owned_reservation(user, reservation_id)
    slot = lock_slot(hint.slot_id)
    reservation = Reservation.objects.select_for_update().get(pk=hint.pk)
    # Terminal holds are already released; acknowledge even if the expiry worker advanced their version.
    if reservation.status in ['cancelled', 'expired']: return reservation_data(reservation)
    if reservation.version != expected_version: raise BookingError('stale_version', 'This hold has changed. Refresh before continuing.')
    if reservation.status != 'held': raise BookingError('state_conflict', 'A confirmed appointment cannot be released as a hold.')
    reservation.status = 'expired' if reservation.expires_at <= timezone.now() else 'cancelled'
    reservation.version += 1; reservation.save(update_fields=['status', 'version'])
    if slot.active_reservation_id == reservation.pk:
        slot.active_reservation = None; slot.version += 1
        slot.save(update_fields=['active_reservation', 'version'])
    fail_pending_change(reservation, 'released')
    audit(user, 'hold.released', reservation.pk, slot.session.facility)
    return reservation_data(reservation)


@transaction.atomic
def confirm(user, hold_id, expected_version, consent_version, reason_category, payment_method, patient_id=None):
    hint = owned_reservation(user, hold_id)
    if patient_id and str(hint.patient_id) != str(patient_id): raise BookingError('not_found', 'Patient does not own this hold.', 404)
    slot = lock_slot(hint.slot_id)
    reservation = Reservation.objects.select_for_update().get(pk=hint.pk)
    now = timezone.now()
    if reservation.expires_at <= now or reservation.status == 'expired':
        raise BookingError('hold_expired', 'Your hold expired. Choose an available time again.', 410)
    if reservation.version != expected_version: raise BookingError('stale_version', 'This hold has changed. Refresh before continuing.')
    if reservation.status != 'held' or slot.active_reservation_id != reservation.pk:
        raise BookingError('state_conflict', 'This hold is no longer available for confirmation.')
    from finance.models import AppointmentChange
    if AppointmentChange.objects.filter(hold=reservation, status='pending_payment').exists():
        raise BookingError('state_conflict', 'This hold is reserved for a pending appointment change.')
    eligible(slot, now)
    if payment_method not in ['counter_due', 'hosted']:
        raise BookingError('unsupported_payment', 'Choose pay at the counter or simulated hosted payment.', 400)
    if consent_version != 'demo-v1' or reason_category not in ['new_visit', 'follow_up', 'routine']:
        raise BookingError('validation', 'Choose a valid booking consent version and visit category.', 400)
    # Consent on the confirmation is explicit for self-service; reception records the patient's agreement.
    Consent.objects.create(patient=reservation.patient, purpose='booking', version=consent_version, accepted=True)
    source = 'patient' if user.role == 'patient' else 'reception'
    snapshot = {**reservation.snapshot, 'consent_version': consent_version, 'consent_recorded_by': source}
    payment_state = 'hosted_pending' if payment_method == 'hosted' else 'counter_due'
    appointment = Appointment.objects.create(reference='SC-' + uuid.uuid4().hex[:20].upper(), reservation=reservation,
        patient=reservation.patient, facility=slot.session.facility, doctor=slot.session.doctor, service=slot.session.service,
        starts_at=slot.starts_at, ends_at=slot.ends_at, source=source, snapshot=snapshot, reason_category=reason_category,
        payment_state=payment_state)
    AppointmentHistory.objects.create(appointment=appointment, actor=user, source=source, from_state='held', to_state='confirmed', version=1)
    reservation.status = 'booked'; reservation.version += 1; reservation.save(update_fields=['status', 'version'])
    slot.version += 1; slot.save(update_fields=['version'])
    checkout = None
    if payment_method == 'hosted':
        from finance.services import open_checkout
        checkout = open_checkout(user, appointment, snapshot['total'], 'appointment')
    audit(user, 'appointment.confirmed', appointment.pk, appointment.facility,
          {'reference': appointment.reference, 'source': source, 'payment_state': payment_state})
    enqueue(f'appointment:{appointment.pk}:confirmed:1', 'appointment.confirmed', {'appointment_id': str(appointment.pk), 'version': 1})
    data = appointment_data(appointment, user)
    if checkout: data['checkout'] = checkout
    return data


def expire_holds():
    count = 0
    ids = list(Reservation.objects.filter(status='held', expires_at__lte=timezone.now()).values_list('slot_id', flat=True)[:500])
    for slot_id in ids:
        with transaction.atomic():
            slot = lock_slot(slot_id)
            before = slot.active_reservation_id
            expire_locked(slot, timezone.now())
            count += bool(before and not slot.active_reservation_id)
    return count
