import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from core.models import AppendOnlyQuerySet


class Checkout(models.Model):
    PURPOSES = [('appointment', 'Appointment payment'), ('reschedule_additional', 'Additional reschedule payment')]
    STATUSES = [(s, s.replace('_', ' ').title()) for s in ['pending', 'succeeded', 'failed', 'expired', 'unmatched']]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    facility = models.ForeignKey('configuration.Facility', on_delete=models.PROTECT)
    appointment = models.ForeignKey('appointments.Appointment', null=True, blank=True, on_delete=models.PROTECT, related_name='checkouts')
    change = models.ForeignKey('AppointmentChange', null=True, blank=True, on_delete=models.PROTECT, related_name='checkouts')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    purpose = models.CharField(max_length=32, choices=PURPOSES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default='LKR')
    status = models.CharField(max_length=16, choices=STATUSES, default='pending')
    provider_reference = models.CharField(max_length=64, unique=True)
    last_event_id = models.CharField(max_length=64, blank=True)
    expires_at = models.DateTimeField()
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['status', 'expires_at'])]


class Payment(models.Model):
    KINDS = [('charge', 'Charge'), ('refund', 'Refund')]
    METHODS = [('counter', 'Counter'), ('hosted', 'Hosted')]
    STATUSES = [(s, s.title()) for s in ['pending', 'succeeded', 'failed']]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    facility = models.ForeignKey('configuration.Facility', on_delete=models.PROTECT)
    appointment = models.ForeignKey('appointments.Appointment', null=True, blank=True, on_delete=models.PROTECT, related_name='payments')
    checkout = models.ForeignKey(Checkout, null=True, blank=True, on_delete=models.PROTECT, related_name='payments')
    obligation = models.ForeignKey('RefundObligation', null=True, blank=True, on_delete=models.PROTECT, related_name='payments')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    kind = models.CharField(max_length=12, choices=KINDS)
    method = models.CharField(max_length=12, choices=METHODS)
    status = models.CharField(max_length=12, choices=STATUSES, default='succeeded')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default='LKR')
    provider_event_id = models.CharField(max_length=64, unique=True)
    provider_reference = models.CharField(max_length=64)
    receipt_number = models.CharField(max_length=40, unique=True, null=True, blank=True)
    snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyQuerySet.as_manager()
    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Payments are append-only.')
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        raise ValidationError('Payments are append-only.')

    class Meta:
        indexes = [models.Index(fields=['appointment', 'created_at']), models.Index(fields=['facility', 'created_at'])]
        constraints = [
            models.UniqueConstraint(fields=['obligation'], name='refund_operation_one_ledger'),
            models.UniqueConstraint(fields=['checkout'], name='checkout_one_ledger'),
        ]


class RefundObligation(models.Model):
    STATUSES = [(s, s.title()) for s in ['pending', 'submitting', 'unknown', 'failed', 'refunded', 'cancelled']]
    REASONS = [('cancellation', 'Cancellation'), ('lower_fee_reschedule', 'Lower-fee reschedule'), ('staff_partial', 'Staff refund')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    facility = models.ForeignKey('configuration.Facility', on_delete=models.PROTECT)
    appointment = models.ForeignKey('appointments.Appointment', on_delete=models.PROTECT, related_name='refund_obligations')
    change = models.ForeignKey('AppointmentChange', null=True, blank=True, on_delete=models.PROTECT, related_name='obligations')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default='LKR')
    status = models.CharField(max_length=12, choices=STATUSES, default='pending')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    source_payment = models.ForeignKey(Payment, null=True, on_delete=models.PROTECT, related_name='refund_operations')
    method = models.CharField(max_length=12, default='counter')
    provider_reference = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    failure_owner = models.CharField(max_length=32, blank=True)
    last_error = models.CharField(max_length=240, blank=True)
    reason = models.CharField(max_length=32, choices=REASONS)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)


class AppointmentChange(models.Model):
    KINDS = [('cancellation', 'Cancellation'), ('reschedule', 'Reschedule')]
    DIRECTIONS = [(s, s.title()) for s in ['none', 'same', 'higher', 'lower']]
    STATUSES = [(s, s.replace('_', ' ').title()) for s in ['pending_payment', 'completed', 'failed', 'cancelled']]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    facility = models.ForeignKey('configuration.Facility', on_delete=models.PROTECT)
    original = models.ForeignKey('appointments.Appointment', on_delete=models.PROTECT, related_name='outgoing_changes')
    replacement = models.ForeignKey('appointments.Appointment', null=True, blank=True, on_delete=models.PROTECT, related_name='incoming_changes')
    hold = models.ForeignKey('appointments.Reservation', null=True, blank=True, on_delete=models.PROTECT, related_name='changes')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=16, choices=KINDS)
    fee_direction = models.CharField(max_length=12, choices=DIRECTIONS, default='none')
    status = models.CharField(max_length=20, choices=STATUSES, default='completed')
    credited_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    additional_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['original', 'status'])]


class FinanceException(models.Model):
    STATUSES = [(s, s.title()) for s in ['open', 'assigned', 'resolved']]
    REASONS = [
        ('late_payment', 'Late payment'),
        ('unmatched', 'Unmatched payment'),
        ('amount_mismatch', 'Amount mismatch'),
        ('malformed', 'Malformed provider event'),
        ('overpayment', 'Overpayment'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    facility = models.ForeignKey('configuration.Facility', null=True, blank=True, on_delete=models.PROTECT)
    appointment = models.ForeignKey('appointments.Appointment', null=True, blank=True, on_delete=models.PROTECT, related_name='finance_exceptions')
    checkout = models.ForeignKey(Checkout, null=True, blank=True, on_delete=models.PROTECT, related_name='exceptions')
    change = models.ForeignKey(AppointmentChange, null=True, blank=True, on_delete=models.PROTECT, related_name='exceptions')
    status = models.CharField(max_length=12, choices=STATUSES, default='open')
    reason = models.CharField(max_length=24, choices=REASONS)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default='LKR')
    provider_event_id = models.CharField(max_length=64, unique=True)
    provider_reference = models.CharField(max_length=64, blank=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='assigned_exceptions')
    note = models.CharField(max_length=240, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['facility', 'status'])]


class ExceptionNote(models.Model):
    exception = models.ForeignKey(FinanceException, on_delete=models.PROTECT, related_name='notes')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    from_status = models.CharField(max_length=12, blank=True)
    to_status = models.CharField(max_length=12)
    note = models.CharField(max_length=240)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = AppendOnlyQuerySet.as_manager()
    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Exception notes are append-only.')
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        raise ValidationError('Exception notes are append-only.')


class GatewayImport(models.Model):
    """Immutable record of a simulated gateway CSV upload."""
    STATUSES = [('pending', 'Pending'), ('processed', 'Processed'), ('failed', 'Failed')]
    facility = models.ForeignKey('configuration.Facility', on_delete=models.PROTECT)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    filename = models.CharField(max_length=255)
    file_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=STATUSES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['facility', 'file_hash'], name='finance_gateway_file_once')]


class GatewayImportRow(models.Model):
    STATUSES = [('matched', 'Matched'), ('unmatched', 'Unmatched'), ('amount_mismatch', 'Amount mismatch'),
                ('invalid', 'Invalid')]
    batch = models.ForeignKey(GatewayImport, on_delete=models.PROTECT, related_name='rows')
    row_number = models.PositiveIntegerField()
    transaction_reference = models.CharField(max_length=128)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    status_value = models.CharField(max_length=32, blank=True)
    provider_event_id = models.CharField(max_length=128, blank=True)
    row_status = models.CharField(max_length=20, choices=STATUSES)
    error = models.CharField(max_length=240, blank=True)
    payment = models.ForeignKey(Payment, null=True, blank=True, on_delete=models.PROTECT, related_name='gateway_rows')
    raw = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['batch', 'row_number'], name='finance_gateway_row_once')]


class DoctorPayable(models.Model):
    STATUSES = [('versioned', 'Versioned'), ('paid', 'Paid'), ('void', 'Void')]
    facility = models.ForeignKey('configuration.Facility', on_delete=models.PROTECT)
    doctor = models.ForeignKey('configuration.Doctor', on_delete=models.PROTECT, related_name='payables')
    appointment = models.OneToOneField('appointments.Appointment', on_delete=models.PROTECT, related_name='doctor_payable')
    source_payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name='doctor_payables')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default='LKR')
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=12, choices=STATUSES, default='versioned')
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        indexes = [models.Index(fields=['facility', 'doctor', 'status'])]


class SettlementMutex(models.Model):
    # A dedicated row avoids locking Facility and interfering with unrelated FK inserts.
    facility = models.OneToOneField('configuration.Facility', primary_key=True, on_delete=models.PROTECT)


class SettlementBatchQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if self.filter(status='paid').exists():
            raise ValidationError('Paid settlements are immutable.')
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError('Settlement history cannot be deleted.')


class SettlementBatch(models.Model):
    STATUSES = [('draft', 'Draft'), ('review', 'Review'), ('approved', 'Approved'), ('paid', 'Paid')]
    facility = models.ForeignKey('configuration.Facility', on_delete=models.PROTECT)
    reference = models.CharField(max_length=128)
    currency = models.CharField(max_length=3, default='LKR')
    adjustment = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=12, choices=STATUSES, default='draft')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='settlement_batches')
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='approved_settlements')
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = SettlementBatchQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if not self._state.adding and type(self).objects.filter(pk=self.pk, status='paid').exists():
            raise ValidationError('Paid settlements are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Settlement history cannot be deleted.')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['facility', 'reference'], name='finance_settlement_reference_once')]


class SettlementLine(models.Model):
    batch = models.ForeignKey(SettlementBatch, on_delete=models.PROTECT, related_name='lines')
    payable = models.ForeignKey(DoctorPayable, null=True, blank=True, on_delete=models.PROTECT, related_name='settlement_lines')
    kind = models.CharField(max_length=12, default='original', choices=[('original', 'Original'), ('refund', 'Refund adjustment')])
    original_payable = models.OneToOneField(DoctorPayable, null=True, blank=True, on_delete=models.PROTECT, related_name='original_allocation')
    original_line = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='refund_adjustments')
    refund = models.OneToOneField(Payment, null=True, blank=True, on_delete=models.PROTECT, related_name='settlement_adjustment')
    doctor = models.ForeignKey('configuration.Doctor', on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    adjustment = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    description = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Settlement lines are immutable.')
        if self.batch.status != 'draft':
            raise ValidationError('Lines can only be added to draft settlements.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Settlement lines are immutable.')

    class Meta:
        constraints = [
            models.CheckConstraint(name='settlement_line_shape', condition=(
                models.Q(kind='original', original_payable__isnull=False,
                         payable=models.F('original_payable'), original_line__isnull=True,
                         refund__isnull=True, amount__gte=0, adjustment=0)
                | models.Q(kind='refund', original_payable__isnull=True,
                           payable__isnull=False, original_line__isnull=False,
                           refund__isnull=False, amount__lt=0, adjustment=0)
            )),
        ]
