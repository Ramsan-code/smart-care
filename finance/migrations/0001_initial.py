import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("accounts", "0002_initial"),
        ("appointments", "0002_initial"),
        ("configuration", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AppointmentChange",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("kind", models.CharField(choices=[("cancellation", "Cancellation"), ("reschedule", "Reschedule")], max_length=16)),
                ("fee_direction", models.CharField(choices=[("none", "None"), ("same", "Same"), ("higher", "Higher"), ("lower", "Lower")], default="none", max_length=12)),
                ("status", models.CharField(choices=[("pending_payment", "Pending Payment"), ("completed", "Completed"), ("failed", "Failed"), ("cancelled", "Cancelled")], default="completed", max_length=20)),
                ("credited_amount", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("additional_amount", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("refund_amount", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ("facility", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="configuration.facility")),
                ("original", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="outgoing_changes", to="appointments.appointment")),
                ("replacement", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="incoming_changes", to="appointments.appointment")),
                ("hold", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="changes", to="appointments.reservation")),
            ],
            options={"indexes": [models.Index(fields=["original", "status"], name="finance_app_original_3d7f5f_idx")]},
        ),
        migrations.CreateModel(
            name="Checkout",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("purpose", models.CharField(choices=[("appointment", "Appointment payment"), ("reschedule_additional", "Additional reschedule payment")], max_length=32)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("currency", models.CharField(default="LKR", max_length=3)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("succeeded", "Succeeded"), ("failed", "Failed"), ("expired", "Expired"), ("unmatched", "Unmatched")], default="pending", max_length=16)),
                ("provider_reference", models.CharField(max_length=64, unique=True)),
                ("last_event_id", models.CharField(blank=True, max_length=64)),
                ("expires_at", models.DateTimeField()),
                ("version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ("appointment", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="checkouts", to="appointments.appointment")),
                ("change", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="checkouts", to="finance.appointmentchange")),
                ("facility", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="configuration.facility")),
            ],
            options={"indexes": [models.Index(fields=["status", "expires_at"], name="finance_chec_status_0a6c66_idx")]},
        ),
        migrations.CreateModel(
            name="FinanceException",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(choices=[("open", "Open"), ("assigned", "Assigned"), ("resolved", "Resolved")], default="open", max_length=12)),
                ("reason", models.CharField(choices=[("late_payment", "Late payment"), ("unmatched", "Unmatched payment"), ("amount_mismatch", "Amount mismatch"), ("malformed", "Malformed provider event"), ("overpayment", "Overpayment")], max_length=24)),
                ("amount", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("currency", models.CharField(default="LKR", max_length=3)),
                ("provider_event_id", models.CharField(max_length=64, unique=True)),
                ("provider_reference", models.CharField(blank=True, max_length=64)),
                ("note", models.CharField(blank=True, max_length=240)),
                ("version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("appointment", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="finance_exceptions", to="appointments.appointment")),
                ("assigned_to", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="assigned_exceptions", to=settings.AUTH_USER_MODEL)),
                ("change", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="exceptions", to="finance.appointmentchange")),
                ("checkout", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="exceptions", to="finance.checkout")),
                ("facility", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="configuration.facility")),
            ],
            options={"indexes": [models.Index(fields=["facility", "status"], name="finance_fin_facility_8a6e03_idx")]},
        ),
        migrations.CreateModel(
            name="RefundObligation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("currency", models.CharField(default="LKR", max_length=3)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("refunded", "Refunded"), ("cancelled", "Cancelled")], default="pending", max_length=12)),
                ("reason", models.CharField(choices=[("cancellation", "Cancellation"), ("lower_fee_reschedule", "Lower-fee reschedule"), ("staff_partial", "Staff refund")], max_length=32)),
                ("version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("appointment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="refund_obligations", to="appointments.appointment")),
                ("change", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="obligations", to="finance.appointmentchange")),
                ("facility", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="configuration.facility")),
            ],
        ),
        migrations.CreateModel(
            name="Payment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("kind", models.CharField(choices=[("charge", "Charge"), ("refund", "Refund")], max_length=12)),
                ("method", models.CharField(choices=[("counter", "Counter"), ("hosted", "Hosted")], max_length=12)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("succeeded", "Succeeded"), ("failed", "Failed")], default="succeeded", max_length=12)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("currency", models.CharField(default="LKR", max_length=3)),
                ("provider_event_id", models.CharField(max_length=64, unique=True)),
                ("provider_reference", models.CharField(max_length=64)),
                ("receipt_number", models.CharField(blank=True, max_length=40, null=True, unique=True)),
                ("snapshot", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ("appointment", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="appointments.appointment")),
                ("checkout", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="finance.checkout")),
                ("facility", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="configuration.facility")),
                ("obligation", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="finance.refundobligation")),
            ],
            options={"indexes": [models.Index(fields=["appointment", "created_at"], name="finance_pay_appointm_8f3d3f_idx"), models.Index(fields=["facility", "created_at"], name="finance_pay_facility_9a2f6a_idx")]},
        ),
        migrations.CreateModel(
            name="ExceptionNote",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("from_status", models.CharField(blank=True, max_length=12)),
                ("to_status", models.CharField(max_length=12)),
                ("note", models.CharField(max_length=240)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ("exception", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="notes", to="finance.financeexception")),
            ],
        ),
    ]
