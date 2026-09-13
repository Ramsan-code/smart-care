from django.db import models


class Delivery(models.Model):
    EVENTS = [
        ("confirmation", "Confirmation"),
        ("changed", "Changed"),
        ("cancelled", "Cancelled"),
        ("reminder", "Reminder"),
    ]
    STATUSES = [
        ("queued", "Queued"),
        ("sent", "Sent"),
        ("failed", "Failed"),
        ("suppressed", "Suppressed"),
    ]
    key = models.CharField(max_length=180, unique=True)
    facility = models.ForeignKey("configuration.Facility", on_delete=models.PROTECT)
    appointment = models.ForeignKey(
        "appointments.Appointment", on_delete=models.PROTECT, related_name="deliveries"
    )
    event = models.CharField(max_length=16, choices=EVENTS)
    channel = models.CharField(max_length=12, default="sms")
    body = models.TextField(blank=True)
    destination = models.CharField(max_length=32)
    masked_destination = models.CharField(max_length=32)
    status = models.CharField(max_length=12, choices=STATUSES, default="queued")
    retry_count = models.PositiveSmallIntegerField(default=0)
    provider_reference = models.CharField(max_length=64, blank=True)
    failure_owner = models.CharField(max_length=32, blank=True)
    last_error = models.CharField(max_length=240, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["facility", "status"]),
            models.Index(fields=["appointment", "event"]),
        ]
