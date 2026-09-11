from celery import shared_task
from django.db import transaction
from django.utils import timezone
from .models import OutboxEvent, ProcessedEvent


@shared_task
def process_event(event_id):
    with transaction.atomic():
        event=OutboxEvent.objects.select_for_update().get(pk=event_id)
        if event.processed_at or event.available_at > timezone.now(): return
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
            delivery = consume_notification_event(event.payload, notification_event)
            if delivery and delivery.status == "failed":
                event.available_at = delivery.next_attempt_at or timezone.now()
                event.save(update_fields=["available_at"])
                return
        elif event.topic == "payment.callback.delayed":
            from finance.services import apply_signed_callback
            apply_signed_callback(
                event.payload["body"].encode(), event.payload["signature"]
            )
        elif event.topic not in {'account.registered','configuration.changed','demo.ping','session.cancelled','payment.recorded','payment.refunded'}:
            raise ValueError('No consumer registered for this event topic.')
        ProcessedEvent.objects.get_or_create(key=event.key,defaults={'result':{'topic':event.topic,'acknowledged':True}})
        event.processed_at=timezone.now()
        event.save(update_fields=['processed_at'])


@shared_task
def dispatch_outbox():
    for event_id in OutboxEvent.objects.filter(processed_at__isnull=True,available_at__lte=timezone.now()).values_list('id',flat=True)[:100]:
        process_event.delay(event_id)
