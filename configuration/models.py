from datetime import date
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models, transaction
from django.db.models import Q


class Organization(models.Model):
    name = models.CharField(max_length=120)
    code = models.SlugField(unique=True)
    def __str__(self): return self.name


class Facility(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    name = models.CharField(max_length=120)
    code = models.SlugField(unique=True)
    timezone = models.CharField(max_length=64, default='Asia/Colombo')
    currency = models.CharField(max_length=3, default='LKR')
    active = models.BooleanField(default=True)
    accepts_registration = models.BooleanField(default=False)
    def __str__(self): return self.name
    def clean(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try: ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError: raise ValidationError({'timezone': 'Unknown time zone.'})
        if self.currency != 'LKR': raise ValidationError({'currency': 'The demo supports LKR only.'})


class FacilityRecord(models.Model):
    facility = models.ForeignKey(Facility, on_delete=models.PROTECT)
    class Meta: abstract = True


class NamedRecord(FacilityRecord):
    name = models.CharField(max_length=120)
    class Meta: abstract = True
    def __str__(self): return self.name


class Department(NamedRecord): pass
class Specialty(NamedRecord): pass
class Room(NamedRecord): pass


class Doctor(NamedRecord):
    user = models.OneToOneField('accounts.User', on_delete=models.PROTECT, blank=True, null=True)
    specialty = models.ForeignKey(Specialty, on_delete=models.PROTECT)
    verified = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    def clean(self):
        if self.specialty_id and self.specialty.facility_id != self.facility_id:
            raise ValidationError({'specialty': 'Specialty must belong to this facility.'})
        if self.user_id and (self.user.role != 'doctor' or not self.user.memberships.filter(facility_id=self.facility_id, active=True).exists()):
            raise ValidationError({'user': 'Select a doctor account assigned to this facility.'})


class Service(NamedRecord):
    duration_minutes = models.PositiveSmallIntegerField(default=15, validators=[MinValueValidator(5), MaxValueValidator(240)])
    active = models.BooleanField(default=True)


class EffectiveRecord(FacilityRecord):
    effective_from = models.DateField(default=date.today)
    effective_until = models.DateField(null=True, blank=True, help_text='Exclusive end date; blank means no end.')
    class Meta: abstract = True
    def clean(self):
        if self.effective_from and self.effective_until and self.effective_until <= self.effective_from:
            raise ValidationError({'effective_until': 'End must be after start.'})


class FeeVersion(EffectiveRecord):
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name='fees')
    doctor_fee = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0'))])
    facility_fee = models.DecimalField(max_digits=12, decimal_places=2, default=500, validators=[MinValueValidator(Decimal('0'))])
    tax = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0'))])
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0'))])
    @property
    def total(self): return self.doctor_fee + self.facility_fee + self.tax - self.discount
    def __str__(self): return f'{self.service} · {self.effective_from} · LKR {self.total:,.2f}'
    def clean(self):
        super().clean()
        if self.service_id and self.service.facility_id != self.facility_id:
            raise ValidationError({'service': 'Service must belong to this facility.'})
        if all(v is not None for v in [self.doctor_fee, self.facility_fee, self.tax, self.discount]) and self.total < 0:
            raise ValidationError({'discount': 'Discount cannot exceed the fee.'})
        if self.service_id and self.effective_from:
            overlaps = FeeVersion.objects.filter(service_id=self.service_id).exclude(pk=self.pk)
            if self._state.adding:
                overlaps=overlaps.exclude(effective_until__isnull=True,effective_from__lt=self.effective_from)
            overlaps = overlaps.filter(Q(effective_until__isnull=True) | Q(effective_until__gt=self.effective_from))
            if self.effective_until: overlaps = overlaps.filter(effective_from__lt=self.effective_until)
            if overlaps.exists(): raise ValidationError('Fee versions cannot have overlapping effective dates.')

    def save(self,*args,**kwargs):
        with transaction.atomic():
            Facility.objects.select_for_update().get(pk=self.facility_id)
            if not self._state.adding: raise ValidationError('Published fee versions are immutable; create a new version.')
            self.full_clean()
            # A later version closes the preceding open interval, without changing its amounts.
            FeeVersion.objects.filter(service_id=self.service_id,effective_until__isnull=True,effective_from__lt=self.effective_from).update(effective_until=self.effective_from)
            return super().save(*args,**kwargs)


class PolicyVersion(EffectiveRecord):
    version = models.CharField(max_length=32)
    hold_minutes = models.PositiveSmallIntegerField(default=5, validators=[MinValueValidator(1), MaxValueValidator(30)])
    cancellation_hours = models.PositiveSmallIntegerField(default=24)
    release_days = models.PositiveSmallIntegerField(default=30, validators=[MinValueValidator(1), MaxValueValidator(365)])
    class Meta:
        constraints = [models.UniqueConstraint(fields=['facility', 'version'], name='unique_policy_version')]
    def __str__(self): return f'{self.facility} · {self.version}'

    def clean(self):
        super().clean()
        if self.facility_id and self.effective_from:
            overlaps = PolicyVersion.objects.filter(facility_id=self.facility_id).exclude(pk=self.pk)
            if self._state.adding:
                overlaps = overlaps.exclude(effective_until__isnull=True, effective_from__lt=self.effective_from)
            overlaps = overlaps.filter(Q(effective_until__isnull=True) | Q(effective_until__gt=self.effective_from))
            if self.effective_until:
                overlaps = overlaps.filter(effective_from__lt=self.effective_until)
            if overlaps.exists():
                raise ValidationError('Policy versions cannot have overlapping effective dates.')

    def save(self, *args, **kwargs):
        with transaction.atomic():
            Facility.objects.select_for_update().get(pk=self.facility_id)
            if not self._state.adding:
                raise ValidationError('Published policy versions are immutable; create a new version.')
            self.full_clean()
            PolicyVersion.objects.filter(facility_id=self.facility_id, effective_until__isnull=True,
                                         effective_from__lt=self.effective_from).update(effective_until=self.effective_from)
            return super().save(*args, **kwargs)


class ScheduleRule(EffectiveRecord):
    doctor = models.ForeignKey(Doctor, on_delete=models.PROTECT)
    service = models.ForeignKey(Service, on_delete=models.PROTECT)
    weekday = models.PositiveSmallIntegerField(choices=list(enumerate(['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'])))
    starts_at = models.TimeField()
    ends_at = models.TimeField()
    break_start = models.TimeField(null=True, blank=True)
    break_end = models.TimeField(null=True, blank=True)
    buffer_minutes = models.PositiveSmallIntegerField(default=0)
    capacity = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(1)])
    def __str__(self): return f'{self.doctor} · {self.get_weekday_display()} {self.starts_at}'
    def clean(self):
        super().clean()
        for field in ['doctor', 'service']:
            if getattr(self, field+'_id') and getattr(self, field).facility_id != self.facility_id:
                raise ValidationError({field: 'Must belong to this facility.'})
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError('Working hours must end after they start.')
        if bool(self.break_start) != bool(self.break_end): raise ValidationError('Set both break start and end.')
        if all([self.starts_at, self.ends_at, self.break_start, self.break_end]) and not (self.starts_at <= self.break_start < self.break_end <= self.ends_at):
            raise ValidationError('Break must be inside working hours.')
        if self.doctor_id and self.starts_at and self.ends_at and self.effective_from:
            qs=ScheduleRule.objects.filter(doctor_id=self.doctor_id, weekday=self.weekday, starts_at__lt=self.ends_at, ends_at__gt=self.starts_at).exclude(pk=self.pk)
            qs=qs.filter(Q(effective_until__isnull=True)|Q(effective_until__gt=self.effective_from))
            if self.effective_until: qs=qs.filter(effective_from__lt=self.effective_until)
            if qs.exists(): raise ValidationError('Doctor working hours overlap another rule.')


class Leave(FacilityRecord):
    doctor = models.ForeignKey(Doctor, on_delete=models.PROTECT)
    starts_on = models.DateField()
    ends_on = models.DateField()
    reason = models.CharField(max_length=100, default='Unavailable')
    def __str__(self): return f'{self.doctor} · {self.starts_on} — {self.ends_on}'
    def clean(self):
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on: raise ValidationError('End cannot be before start.')
        if self.doctor_id and self.doctor.facility_id != self.facility_id: raise ValidationError('Doctor must belong to this facility.')


class MessageTemplate(FacilityRecord):
    event = models.CharField(max_length=40, choices=[('booking_confirmed','Booking confirmation'),('appointment_changed','Appointment change'),('appointment_cancelled','Cancellation'),('reminder','Reminder')])
    language = models.CharField(max_length=8, default='en')
    body = models.TextField(help_text='Use {reference}, {date}, {time}, {facility}. No clinical information.')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['facility','event','language'], name='unique_message_template')]
    def __str__(self): return f'{self.get_event_display()} · {self.language}'
    def clean(self):
        import string
        allowed={'reference','date','time','facility'}
        try: fields={field for _,field,_,_ in string.Formatter().parse(self.body) if field is not None}
        except ValueError: raise ValidationError({'body':'Invalid template braces.'})
        if not fields <= allowed: raise ValidationError({'body':'Only reference, date, time and facility placeholders are allowed.'})
