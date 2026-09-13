import uuid
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs): raise ValidationError('Audit history is append-only.')
    def delete(self): raise ValidationError('Audit history is append-only.')


class AuditEvent(models.Model):
    facility = models.ForeignKey('configuration.Facility', null=True, on_delete=models.PROTECT)
    actor = models.ForeignKey('accounts.User', null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=80)
    target = models.CharField(max_length=150)
    detail = models.JSONField(default=dict)
    correlation_id = models.UUIDField(default=uuid.uuid4)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = AppendOnlyQuerySet.as_manager()
    def save(self,*args,**kwargs):
        if not self._state.adding: raise ValidationError('Audit history is append-only.')
        return super().save(*args,**kwargs)
    def delete(self,*args,**kwargs): raise ValidationError('Audit history is append-only.')


class OutboxEvent(models.Model):
    key = models.CharField(max_length=160, unique=True)
    topic = models.CharField(max_length=80)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=240, blank=True)
    failure_owner = models.CharField(max_length=32, blank=True)
    dead_lettered_at = models.DateTimeField(null=True, blank=True)
    payload = models.JSONField(default=dict)
    available_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta: indexes=[models.Index(fields=['processed_at','available_at'])]


class ProcessedEvent(models.Model):
    key = models.CharField(max_length=160, unique=True)
    result = models.JSONField(default=dict)
    processed_at = models.DateTimeField(auto_now_add=True)


class IdempotencyRecord(models.Model):
    actor = models.ForeignKey('accounts.User', on_delete=models.PROTECT)
    operation = models.CharField(max_length=80)
    key = models.CharField(max_length=100)
    request_hash = models.CharField(max_length=64)
    response = models.JSONField(null=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['actor','operation','key'],name='unique_idempotency_command')]
