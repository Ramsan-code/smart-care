from django.db import transaction
from django.utils import timezone

from accounts.permissions import facility_ids
from core.services import audit, enqueue
from scheduling.models import Session
from scheduling.services import BookingError
from .models import Appointment, AppointmentHistory, CancellationResolution, SessionCancellation
from .services import lock_slot


TRANSITIONS = {
    "confirmed": {"arrived": ["reception", "administrator"], "no_show": ["reception", "administrator"]},
    "arrived": {"waiting": ["reception", "administrator"], "left": ["reception", "administrator"]},
    "waiting": {"in_consultation": ["doctor", "administrator"], "left": ["reception", "doctor", "administrator"]},
    "in_consultation": {"completed": ["doctor", "administrator"], "left": ["reception", "doctor", "administrator"]},
}


def can_operate(user, appointment):
    if user.role == "administrator":
        return True
    if appointment.facility_id not in set(facility_ids(user)):
        raise BookingError("forbidden", "This appointment is outside your facility scope.", 403)
    if user.role == "doctor" and appointment.doctor.user_id != user.pk:
        raise BookingError("forbidden", "Doctors can only update their own appointments.", 403)
    if user.role not in ["reception", "doctor"]:
        raise BookingError("forbidden", "Outpatient operations access is required.", 403)
    return True


@transaction.atomic
def transition_appointment(user, appointment_id, expected_version, to_state, note=""):
    from finance.services import accessible_appointment
    from finance.services import set_state
    appointment = accessible_appointment(user, appointment_id)
    can_operate(user, appointment)
    appointment = Appointment.objects.select_for_update().select_related("doctor", "facility").get(pk=appointment.pk)
    if appointment.version != expected_version:
        raise BookingError("stale_version", "This appointment has changed. Refresh before updating.")
    from_state = appointment.state
    allowed = TRANSITIONS.get(from_state, {})
    if to_state not in allowed or user.role not in allowed[to_state] and user.role != "administrator":
        raise BookingError("state_conflict", f"Cannot move {appointment.state} to {to_state}.", 409)
    set_state(appointment, user, to_state, user.role)
    audit(user, "appointment.operation_updated", appointment.pk, appointment.facility,
          {"from": from_state, "to": to_state, "note": note[:240]})
    enqueue(
        f"appointment:{appointment.pk}:changed:{appointment.version}",
        "appointment.changed",
        {"appointment_id": str(appointment.pk), "version": appointment.version},
    )
    return {"id": str(appointment.pk), "state": to_state, "version": appointment.version}


@transaction.atomic
def cancel_session(user, session_id, expected_version, reason):
    from finance.services import release_capacity, set_state
    session = Session.objects.select_for_update().select_related("facility").filter(pk=session_id).first()
    if not session:
        raise BookingError("not_found", "Session not found.", 404)
    if user.role not in ["reception", "administrator"] or (
        user.role != "administrator" and session.facility_id not in set(facility_ids(user))
    ):
        raise BookingError("forbidden", "Session operations access is required.", 403)
    if session.version != expected_version:
        raise BookingError("stale_version", "This session has changed. Refresh before cancelling.")
    if hasattr(session, "cancellation"):
        raise BookingError("state_conflict", "This session is already cancelled.", 409)
    cancellation = SessionCancellation.objects.create(
        session=session, facility=session.facility, actor=user, reason=reason[:240]
    )
    session.active = False
    session.version += 1
    session.save(update_fields=["active", "version"])
    appointments = list(
        Appointment.objects.select_for_update().select_related("reservation", "facility").filter(
            reservation__slot__session=session, state="confirmed"
        )
    )
    resolutions = []
    for appointment in appointments:
        paid = None
        try:
            from finance.services import net_paid, execute_refund
            paid = net_paid(appointment)
        except ImportError:
            paid = 0
        release_capacity(lock_slot(appointment.reservation.slot_id), appointment.reservation)
        set_state(appointment, user, "cancelled", "session")
        if paid and paid > 0:
            execute_refund(user, appointment, paid, "cancellation", method="hosted" if appointment.payments.filter(method="hosted", kind="charge").exists() else "counter")
        resolution = CancellationResolution.objects.create(cancellation=cancellation, appointment=appointment)
        resolutions.append(resolution.pk)
        enqueue(
            f"appointment:{appointment.pk}:cancelled:{appointment.version}",
            "appointment.cancelled",
            {"appointment_id": str(appointment.pk), "version": appointment.version},
        )
    audit(user, "session.cancelled", session.pk, session.facility,
          {"affected": len(appointments), "reason": reason[:240]})
    enqueue(
        f"session:{session.pk}:cancelled:{session.version}",
        "session.cancelled",
        {"session_id": str(session.pk), "affected": len(appointments)},
    )
    return {"session_id": str(session.pk), "version": session.version,
            "affected": len(appointments), "resolution_ids": [str(pk) for pk in resolutions]}


@transaction.atomic
def resolve_cancellation(user, resolution_id, expected_version, status, outcome="", note=""):
    resolution = CancellationResolution.objects.select_related(
        "appointment", "cancellation__facility"
    ).filter(pk=resolution_id).first()
    if not resolution:
        raise BookingError("not_found", "Cancellation resolution not found.", 404)
    if user.role not in ["reception", "administrator"] or (
        user.role != "administrator" and resolution.cancellation.facility_id not in set(facility_ids(user))
    ):
        raise BookingError("forbidden", "Session operations access is required.", 403)
    if resolution.version != expected_version:
        raise BookingError("stale_version", "This resolution has changed. Refresh before updating.")
    if status not in dict(CancellationResolution.STATUSES):
        raise BookingError("validation", "Choose a valid resolution status.", 400)
    if status == "resolved" and outcome not in dict(CancellationResolution.OUTCOMES):
        raise BookingError("validation", "Choose a valid resolution outcome.", 400)
    resolution.status = status
    resolution.outcome = outcome if status == "resolved" else ""
    resolution.note = note[:240]
    resolution.version += 1
    resolution.save(update_fields=["status", "outcome", "note", "version", "updated_at"])
    audit(user, "session.cancellation_resolution_updated", resolution.pk,
          resolution.cancellation.facility, {"status": status, "outcome": outcome})
    return {"id": str(resolution.pk), "status": resolution.status,
            "outcome": resolution.outcome, "note": resolution.note, "version": resolution.version}
