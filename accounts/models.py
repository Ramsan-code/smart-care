import uuid
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.exceptions import ValidationError
from django.db import models


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError('Email is required.')
        user = self.model(email=email.strip().lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.update(is_staff=True, is_superuser=True, role='administrator', email_verified=True)
        return self.create_user(email, password, **extra)


class User(AbstractUser):
    class Role(models.TextChoices):
        PATIENT = 'patient', 'Patient'
        RECEPTION = 'reception', 'Reception'
        DOCTOR = 'doctor', 'Doctor'
        FINANCE = 'finance', 'Finance'
        ADMIN = 'administrator', 'Administrator'
    username = None
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.PATIENT)
    email_verified = models.BooleanField(default=False)
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []
    objects = UserManager()

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.get_full_name() or self.email


class Membership(models.Model):
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name='memberships')
    facility = models.ForeignKey('configuration.Facility', on_delete=models.PROTECT)
    active = models.BooleanField(default=True)
    finance_approver = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'facility'], name='unique_membership')]

    def clean(self):
        if self.finance_approver and self.user.role != User.Role.FINANCE:
            raise ValidationError('Only finance users can be settlement approvers.')


class Patient(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, null=True, blank=True, on_delete=models.PROTECT, related_name='patient')
    facility = models.ForeignKey('configuration.Facility', on_delete=models.PROTECT)
    name = models.CharField(max_length=160)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    assistance = models.CharField(max_length=30, choices=[('', 'None requested'), ('mobility', 'Mobility assistance'), ('communication', 'Communication assistance')], blank=True)
    sms_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Consent(models.Model):
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name='consents')
    version = models.CharField(max_length=32)
    purpose = models.CharField(max_length=40, default='booking')
    accepted = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)


class DemoEmail(models.Model):
    recipient = models.EmailField()
    subject = models.CharField(max_length=255)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
