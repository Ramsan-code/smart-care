from datetime import date,time
from decimal import Decimal
from django.test import TestCase
from django.core.exceptions import ValidationError
from .models import Organization,Facility,Specialty,Doctor,Service,ScheduleRule,FeeVersion,MessageTemplate,Leave,PolicyVersion

class ConfigurationTests(TestCase):
    def setUp(self):
        org=Organization.objects.create(code='A',name='A')
        self.site=Facility.objects.create(organization=org,code='A',name='A')
        self.other=Facility.objects.create(organization=org,code='B',name='B')
        self.specialty=Specialty.objects.create(facility=self.site,name='General')
        self.doctor=Doctor.objects.create(facility=self.site,specialty=self.specialty,name='Doctor')
        self.service=Service.objects.create(facility=self.site,name='Consultation')
    def rule(self,**kwargs):
        values=dict(facility=self.site,doctor=self.doctor,service=self.service,weekday=0,starts_at=time(9),ends_at=time(12),effective_from=date(2026,9,11))
        values.update(kwargs);return ScheduleRule(**values)
    def test_break_must_be_inside_working_hours(self):
        with self.assertRaises(ValidationError): self.rule(break_start=time(8),break_end=time(8,15)).full_clean()
    def test_rule_rejects_reversed_hours(self):
        with self.assertRaises(ValidationError): self.rule(ends_at=time(8)).full_clean()
    def test_rule_rejects_foreign_service(self):
        foreign=Service.objects.create(facility=self.other,name='Foreign')
        with self.assertRaises(ValidationError): self.rule(service=foreign).full_clean()
    def test_rule_overlap_rejected(self):
        self.rule().save()
        with self.assertRaises(ValidationError): self.rule(starts_at=time(10)).full_clean()
        self.rule(starts_at=time(12),ends_at=time(13)).full_clean()
    def test_effective_end_exclusive(self):
        self.rule(effective_until=date(2026,10,1)).save()
        self.rule(effective_from=date(2026,10,1)).full_clean()
    def test_fee_versions_snapshot_and_close_previous_interval(self):
        first=FeeVersion.objects.create(facility=self.site,service=self.service,effective_from=date(2026,9,11),doctor_fee=Decimal('2000'))
        second=FeeVersion.objects.create(facility=self.site,service=self.service,effective_from=date(2026,10,1),doctor_fee=Decimal('3000'))
        first.refresh_from_db();self.assertEqual(first.total,Decimal('2500'));self.assertEqual(first.effective_until,date(2026,10,1));self.assertEqual(second.total,Decimal('3500'))
        first.doctor_fee=4000
        with self.assertRaises(ValidationError): first.save()
    def test_same_start_fee_rejected(self):
        FeeVersion.objects.create(facility=self.site,service=self.service,effective_from=date(2026,9,11),doctor_fee=2000)
        with self.assertRaises(ValidationError): FeeVersion.objects.create(facility=self.site,service=self.service,effective_from=date(2026,9,11),doctor_fee=3000)
    def test_fee_cannot_be_negative(self):
        with self.assertRaises(ValidationError): FeeVersion(facility=self.site,service=self.service,doctor_fee=100,facility_fee=500,tax=0,discount=1000).full_clean()
    def test_template_rejects_arbitrary_placeholder(self):
        with self.assertRaises(ValidationError): MessageTemplate(facility=self.site,event='reminder',body='{patient.password}').full_clean()
    def test_doctor_cannot_reference_foreign_specialty(self):
        foreign=Specialty.objects.create(facility=self.other,name='Private')
        with self.assertRaises(ValidationError): Doctor(facility=self.site,specialty=foreign,name='Test').full_clean()
    def test_incomplete_leave_is_validation_error_not_crash(self):
        with self.assertRaises(ValidationError): Leave(facility=self.site,doctor=self.doctor,starts_on=None,ends_on=None).full_clean()
    def test_incomplete_working_hours_is_validation_error_not_crash(self):
        with self.assertRaises(ValidationError): self.rule(starts_at=None,break_start=time(10),break_end=time(10,15)).full_clean()
    def test_missing_fee_effective_date_is_validation_error_not_crash(self):
        with self.assertRaises(ValidationError): FeeVersion(facility=self.site,service=self.service,effective_from=None,doctor_fee=1000).full_clean()
    def test_policy_versions_are_immutable_and_nonoverlapping(self):
        first=PolicyVersion.objects.create(facility=self.site,version='v1',effective_from=date(2026,9,11))
        second=PolicyVersion.objects.create(facility=self.site,version='v2',effective_from=date(2026,10,1),hold_minutes=10)
        first.refresh_from_db();self.assertEqual(first.effective_until,second.effective_from)
        first.hold_minutes=20
        with self.assertRaises(ValidationError): first.save()
        with self.assertRaises(ValidationError): PolicyVersion.objects.create(facility=self.site,version='v3',effective_from=date(2026,10,1))
