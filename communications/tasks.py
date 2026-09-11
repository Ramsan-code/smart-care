from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from appointments.models import Appointment
from core.services import enqueue


@shared_task
def queue_due_reminders():
    now = timezone.now()
    window_end = now + timedelta(minutes=15)
    appointments = Appointment.objects.filter(
        state="confirmed",
        starts_at__gt=now,
        starts_at__lte=window_end,
    ).select_related("patient")
    count = 0
    for appointment in appointments:
        if not appointment.patient.phone:
            continue
        key = f"appointment:{appointment.pk}:reminder"
        enqueue(
            key,
            "appointment.reminder",
            {"appointment_id": str(appointment.pk), "version": appointment.version},
        )
        count += 1
    return count
