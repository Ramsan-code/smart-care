import uuid
from django.db import models


class Session(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rule = models.ForeignKey('configuration.ScheduleRule', on_delete=models.PROTECT)
    facility = models.ForeignKey('configuration.Facility', on_delete=models.PROTECT)
    doctor = models.ForeignKey('configuration.Doctor', on_delete=models.PROTECT)
    service = models.ForeignKey('configuration.Service', on_delete=models.PROTECT)
    date = models.DateField()
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['rule', 'date'], name='unique_rule_session_date')]
        indexes = [models.Index(fields=['doctor', 'starts_at', 'ends_at'])]


class Slot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(Session, on_delete=models.PROTECT, related_name='slots')
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    position = models.PositiveSmallIntegerField(default=1)
    blocked = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    active_reservation = models.OneToOneField('appointments.Reservation', null=True, blank=True,
                                              on_delete=models.PROTECT, related_name='active_slot')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['session', 'starts_at', 'position'], name='unique_session_position')]
        ordering = ['starts_at', 'id']
