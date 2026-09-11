from celery import shared_task
from django.db import transaction
from django.utils import timezone
from .models import OutboxEvent, ProcessedEvent


@shared_task
def process_event(event_id):
    with transaction.atomic():
        event=OutboxEvent.objects.select_for_update().get(pk=event_id)
        if event.processed_at or event.available_at > timezone.now(): return
        # Phase 2 consumer contract: record durable acknowledgements, no SMS/payment side effects.
        if event.topic not in {'account.registered','configuration.changed','demo.ping','appointment.confirmed'}:
            raise ValueError('No consumer registered for this event topic.')
        ProcessedEvent.objects.get_or_create(key=event.key,defaults={'result':{'topic':event.topic,'acknowledged':True}})
        event.processed_at=timezone.now()
        event.save(update_fields=['processed_at'])


@shared_task
def dispatch_outbox():
    for event_id in OutboxEvent.objects.filter(processed_at__isnull=True,available_at__lte=timezone.now()).values_list('id',flat=True)[:100]:
        process_event.delay(event_id)
