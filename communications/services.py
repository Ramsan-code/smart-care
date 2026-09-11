from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from configuration.models import MessageTemplate
from core.services import audit
from .adapters import adapter
from .models import Delivery


def mask_destination(value):
    value = value or ""
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


def scoped_deliveries(user):
    qs = Delivery.objects.select_related("appointment", "facility")
    if user.role == "patient":
        return qs.filter(appointment__patient__user=user)
    if user.role in ["reception", "finance", "administrator", "doctor"]:
        from accounts.permissions import facility_ids
        return qs.filter(facility_id__in=facility_ids(user))
    return qs.none()


def delivery_data(delivery):
    return {
        "id": delivery.pk,
        "appointment_id": str(delivery.appointment_id),
        "reference": delivery.appointment.reference,
        "event": delivery.event,
        "channel": delivery.channel,
        "destination": delivery.masked_destination,
        "status": delivery.status,
        "retry_count": delivery.retry_count,
        "provider_reference": delivery.provider_reference,
        "failure_owner": delivery.failure_owner,
        "last_error": delivery.last_error,
        "created_at": delivery.created_at.isoformat(),
        "sent_at": delivery.sent_at.isoformat() if delivery.sent_at else None,
    }


def template_text(appointment, event):
    template = MessageTemplate.objects.filter(
        facility=appointment.facility, event=event, language="en"
    ).first()
    body = template.body if template else (
        "Smart Care: appointment {reference} at {date} {time} at {facility}."
    )
    local = timezone.localtime(appointment.starts_at)
    return body.format(
        reference=appointment.reference,
        date=local.strftime("%d %b %Y"),
        time=local.strftime("%H:%M"),
        facility=appointment.facility.name,
    )


def queue_delivery(appointment, event):
    destination = appointment.patient.phone
    if not destination:
        return None
    key = f"sms:{appointment.pk}:{event}:{appointment.version}"
    delivery, _ = Delivery.objects.get_or_create(
        key=key,
        defaults={
            "facility": appointment.facility,
            "appointment": appointment,
            "event": event,
            "destination": destination,
            "masked_destination": mask_destination(destination),
        },
    )
    return delivery


@transaction.atomic
def consume_notification_event(payload, event):
    from appointments.models import Appointment

    appointment = Appointment.objects.select_for_update().select_related(
        "patient", "facility"
    ).get(pk=payload["appointment_id"])
    if event == "reminder" and appointment.state != "confirmed":
        delivery = queue_delivery(appointment, event)
        if delivery and delivery.status not in ["sent", "suppressed"]:
            delivery.status = "suppressed"
            delivery.failure_owner = "operations"
            delivery.last_error = "Appointment is no longer confirmed."
            delivery.save(update_fields=["status", "failure_owner", "last_error"])
        return delivery
    delivery = queue_delivery(appointment, event)
    if not delivery:
        return None
    return send_delivery(delivery, appointment)


def send_delivery(delivery, appointment=None):
    delivery = Delivery.objects.select_for_update().get(pk=delivery.pk)
    if delivery.status in ["sent", "suppressed"]:
        return delivery
    if appointment is None:
        appointment = delivery.appointment
    result = adapter().send(
        key=delivery.key,
        destination=delivery.destination,
        text=template_text(appointment, delivery.event),
    )
    delivery.retry_count += 1
    if result.status == "succeeded":
        delivery.status = "sent"
        delivery.provider_reference = result.reference
        delivery.sent_at = timezone.now()
        delivery.failure_owner = ""
        delivery.last_error = ""
        delivery.next_attempt_at = None
    else:
        delivery.status = "failed"
        delivery.failure_owner = "communications"
        delivery.last_error = "SMS provider did not accept the message."
        delivery.next_attempt_at = timezone.now() + timedelta(minutes=5)
    delivery.save(
        update_fields=[
            "retry_count", "status", "provider_reference", "sent_at",
            "failure_owner", "last_error", "next_attempt_at",
        ]
    )
    audit(None, f"sms.{delivery.status}", delivery.pk, delivery.facility,
          {"appointment_id": str(delivery.appointment_id), "retry_count": delivery.retry_count})
    return delivery


def retry_delivery(user, delivery_id):
    delivery = scoped_deliveries(user).filter(pk=delivery_id).first()
    if not delivery:
        from scheduling.services import BookingError
        raise BookingError("not_found", "Notification delivery not found.", 404)
    if delivery.status != "failed":
        from scheduling.services import BookingError
        raise BookingError("state_conflict", "Only failed notifications can be retried.", 409)
    return delivery_data(send_delivery(delivery))
