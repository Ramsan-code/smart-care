import uuid
from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError
from core.models import AppendOnlyQuerySet


class Reservation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slot = models.ForeignKey('scheduling.Slot', on_delete=models.PROTECT, related_name='reservations')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    patient = models.ForeignKey('accounts.Patient', on_delete=models.PROTECT)
    status = models.CharField(max_length=12, choices=[(s, s.title()) for s in ['held', 'booked', 'expired', 'cancelled']], default='held')
    expires_at = models.DateTimeField()
    version = models.PositiveIntegerField(default=1)
    snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['status', 'expires_at'])]


class AppointmentQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if set(kwargs) & {'snapshot', 'reference', 'patient', 'patient_id', 'reservation', 'reservation_id', 'starts_at', 'ends_at', 'doctor', 'doctor_id', 'facility', 'facility_id', 'service', 'service_id', 'source', 'reason_category'}:
            raise ValidationError('Booking details are immutable; use a linked replacement appointment.')
        return super().update(**kwargs)
    def delete(self): raise ValidationError('Appointment history cannot be deleted.')


class Appointment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=40, unique=True)
    reservation = models.OneToOneField(Reservation, on_delete=models.PROTECT)
    patient = models.ForeignKey('accounts.Patient', on_delete=models.PROTECT)
    facility = models.ForeignKey('configuration.Facility', on_delete=models.PROTECT)
    doctor = models.ForeignKey('configuration.Doctor', on_delete=models.PROTECT)
    service = models.ForeignKey('configuration.Service', on_delete=models.PROTECT)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    state = models.CharField(max_length=24, default='confirmed')
    payment_state = models.CharField(max_length=24, default='counter_due')
    source = models.CharField(max_length=16)
    version = models.PositiveIntegerField(default=1)
    snapshot = models.JSONField()
    reason_category = models.CharField(max_length=24)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def reason_label(self):
        return {'new_visit':'New visit','follow_up':'Follow-up','routine':'Routine consultation'}.get(self.reason_category,self.reason_category)

    objects = AppointmentQuerySet.as_manager()
    def save(self, *args, **kwargs):
        if not self._state.adding:
            old = Appointment.objects.get(pk=self.pk)
            fields = ['snapshot','reference','patient_id','reservation_id','starts_at','ends_at','doctor_id','facility_id','service_id','source','reason_category']
            if any(getattr(old, f) != getattr(self, f) for f in fields):
                raise ValidationError('Booking details are immutable; create a linked replacement.')
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs): raise ValidationError('Appointment history cannot be deleted.')

    class Meta:
        indexes = [models.Index(fields=['facility', 'starts_at', 'state']), models.Index(fields=['doctor', 'starts_at'])]
        ordering = ['-starts_at', '-id']


class AppointmentHistory(models.Model):
    appointment = models.ForeignKey(Appointment, on_delete=models.PROTECT, related_name='history')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    source = models.CharField(max_length=16)
    from_state = models.CharField(max_length=24, blank=True)
    to_state = models.CharField(max_length=24)
    version = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyQuerySet.as_manager()
    def save(self, *args, **kwargs):
        if not self._state.adding: raise ValidationError('Appointment history is append-only.')
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs): raise ValidationError('Appointment history is append-only.')
