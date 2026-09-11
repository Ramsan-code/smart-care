from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from threading import Barrier
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings, tag
from rest_framework.test import APIClient
from accounts.models import User, Patient, Membership, Consent
from configuration.models import Organization, Facility, Specialty, Doctor, Service, FeeVersion, PolicyVersion, ScheduleRule, Leave
from scheduling.models import Session, Slot
from scheduling.services import generate, BookingError
from core.models import AuditEvent, OutboxEvent
from core.tasks import process_event
from .models import Reservation, Appointment, AppointmentHistory
from .services import hold, confirm, release, expire_holds, command

NOW = datetime(2026, 9, 13, 2, tzinfo=dt_timezone.utc)
DAY = date(2026, 9, 14)


class BookingFixture:
    def setUp(self):
        super().setUp()
        clock_patch = patch('django.utils.timezone.now', return_value=NOW)
        self.clock = clock_patch.start(); self.addCleanup(clock_patch.stop)
        org = Organization.objects.create(name='Clinic', code='test')
        self.facility = Facility.objects.create(organization=org, name='Main', code='main', accepts_registration=True)
        self.other = Facility.objects.create(organization=org, name='Other', code='other')
        specialty = Specialty.objects.create(facility=self.facility, name='General')
        self.doctor_user = User.objects.create_user('doctor@example.test', role='doctor')
        Membership.objects.create(user=self.doctor_user, facility=self.facility)
        self.doctor = Doctor.objects.create(facility=self.facility, specialty=specialty, name='Dr Demo', user=self.doctor_user, verified=True)
        self.service = Service.objects.create(facility=self.facility, name='Consultation', duration_minutes=15)
        self.fee = FeeVersion.objects.create(facility=self.facility, service=self.service, effective_from=date(2026, 9, 1), doctor_fee=2000)
        self.policy = PolicyVersion.objects.create(facility=self.facility, version='v1', effective_from=date(2026, 9, 1))
        self.rule = ScheduleRule.objects.create(facility=self.facility, doctor=self.doctor, service=self.service, weekday=0,
            starts_at=time(9), ends_at=time(12), break_start=time(10), break_end=time(10,15), effective_from=date(2026,9,1))
        self.users=[]; self.patients=[]
        for i in range(2):
            user=User.objects.create_user(f'patient{i}@example.test', email_verified=True)
            patient=Patient.objects.create(user=user,facility=self.facility,name=f'Patient {i}')
            Consent.objects.create(patient=patient,version='demo-v1',accepted=True)
            self.users.append(user);self.patients.append(patient)
        self.reception=User.objects.create_user('reception@example.test',role='reception')
        self.membership=Membership.objects.create(user=self.reception,facility=self.facility)
        self.admin=User.objects.create_user('admin@example.test',role='administrator')
        Membership.objects.create(user=self.admin,facility=self.facility)
        self.session,_=generate(self.rule.pk,DAY)
        self.slot=self.session.slots.first()
        self.api=APIClient();self.api.force_authenticate(self.users[0])

    def make_hold(self, user=None):
        self.slot.refresh_from_db()
        return hold(user or self.users[0],self.slot.pk,self.slot.version)

    def confirm_hold(self,h,user=None):
        return confirm(user or self.users[0],h['id'],h['version'],'demo-v1','new_visit','counter_due')

    def post_hold(self, client=None, key='hold1', **overrides):
        body={'slot_id':str(self.slot.pk),'expected_version':self.slot.version,**overrides}
        return (client or self.api).post('/api/v1/holds/',body,format='json',HTTP_IDEMPOTENCY_KEY=key)


@override_settings(DEBUG=True, DEMO_MODE=True, CACHES={'default':{'BACKEND':'django.core.cache.backends.locmem.LocMemCache'}})
class BookingTests(BookingFixture, TestCase):
    def test_generation_is_repeatable_with_break(self):
        session,created=generate(self.rule.pk,DAY)
        self.assertFalse(created);self.assertEqual(session.pk,self.session.pk)
        self.assertEqual(session.slots.count(),11)
        from zoneinfo import ZoneInfo
        starts=[s.starts_at.astimezone(ZoneInfo('Asia/Colombo')).time() for s in session.slots.all()]
        self.assertNotIn(time(10),starts);self.assertIn(time(10,15),starts)

    def test_leave_and_effective_dates(self):
        Leave.objects.create(facility=self.facility,doctor=self.doctor,starts_on=DAY+timedelta(days=7),ends_on=DAY+timedelta(days=7))
        self.assertEqual(generate(self.rule.pk,DAY+timedelta(days=7)),(None,False))
        self.assertEqual(generate(self.rule.pk,date(2026,8,31)),(None,False))
        self.assertEqual(generate(self.rule.pk,DAY+timedelta(days=1)),(None,False))

    def test_cross_service_published_overlap_rejected(self):
        other=Service.objects.create(facility=self.facility,name='Other service')
        # Existing published inventory still prevents overlap after a source rule is edited.
        self.rule.starts_at=time(13);self.rule.ends_at=time(14);self.rule.break_start=None;self.rule.break_end=None;self.rule.save()
        alternate=ScheduleRule.objects.create(facility=self.facility,doctor=self.doctor,service=other,weekday=0,
            starts_at=time(9),ends_at=time(11),effective_from=DAY)
        with self.assertRaises(BookingError):generate(alternate.pk,DAY)
        self.assertEqual(Session.objects.count(),1)

    def test_generation_buffer_and_published_snapshot(self):
        self.rule.buffer_minutes=5;self.rule.save()
        later,_=generate(self.rule.pk,DAY+timedelta(days=7))
        slots=list(later.slots.all())
        self.assertEqual(slots[1].starts_at-slots[0].starts_at,timedelta(minutes=20))
        old=self.session.slots.count();generate(self.rule.pk,DAY)
        self.assertEqual(self.session.slots.count(),old)
        self.assertEqual(self.session.snapshot['buffer_minutes'],0)

    def test_hold_then_confirm_snapshot_history_outbox(self):
        h=self.make_hold();a=self.confirm_hold(h)
        self.assertEqual(a['state'],'confirmed');self.assertEqual(a['payment_state'],'counter_due')
        self.assertEqual(a['snapshot']['total'],'2500.00')
        self.slot.refresh_from_db();self.assertEqual(str(self.slot.active_reservation_id),h['id'])
        self.assertEqual(Reservation.objects.get(pk=h['id']).status,'booked')
        self.assertEqual(AppointmentHistory.objects.count(),1)
        self.assertTrue(AuditEvent.objects.filter(action='appointment.confirmed').exists())
        event=OutboxEvent.objects.get(topic='appointment.confirmed');process_event(event.pk);process_event(event.pk)
        event.refresh_from_db();self.assertIsNotNone(event.processed_at)

    def test_snapshot_and_history_are_immutable(self):
        result=self.confirm_hold(self.make_hold());a=Appointment.objects.get(pk=result['id'])
        FeeVersion.objects.create(facility=self.facility,service=self.service,effective_from=DAY,doctor_fee=3000)
        a.refresh_from_db();self.assertEqual(a.snapshot['total'],'2500.00')
        a.snapshot['total']='1.00'
        with self.assertRaises(ValidationError):a.save()
        with self.assertRaises(ValidationError):Appointment.objects.filter(pk=a.pk).update(snapshot={})
        with self.assertRaises(ValidationError):AppointmentHistory.objects.all().delete()

    def test_confirmation_keeps_hold_price(self):
        h=self.make_hold()
        FeeVersion.objects.create(facility=self.facility,service=self.service,effective_from=DAY,doctor_fee=3000)
        self.assertEqual(self.confirm_hold(h)['snapshot']['total'],'2500.00')

    def test_exact_expiry_cannot_confirm_and_can_rebook_without_worker(self):
        h=self.make_hold();self.clock.return_value=datetime.fromisoformat(h['expires_at'])
        with self.assertRaises(BookingError) as exc:self.confirm_hold(h)
        self.assertEqual(exc.exception.status,410)
        h2=self.make_hold(self.users[1]);self.assertNotEqual(h['id'],h2['id'])
        self.assertEqual(Reservation.objects.get(pk=h['id']).status,'expired')
        with self.assertRaises(BookingError):self.confirm_hold(h)
        self.assertEqual(Appointment.objects.count(),0)

    def test_expiry_worker_is_repeatable(self):
        h=self.make_hold();self.clock.return_value=datetime.fromisoformat(h['expires_at'])
        self.assertEqual(expire_holds(),1);self.assertEqual(expire_holds(),0)
        self.slot.refresh_from_db();self.assertIsNone(self.slot.active_reservation_id)

    def test_release_acknowledges_worker_expired_hold_with_original_version(self):
        h=self.make_hold();self.clock.return_value=datetime.fromisoformat(h['expires_at'])
        expire_holds()
        result=release(self.users[0],h['id'],h['version'])
        self.assertEqual(result['status'],'expired')
        self.slot.refresh_from_db();self.assertIsNone(self.slot.active_reservation_id)

    def test_release_preserves_history_and_reopens_capacity(self):
        h=self.make_hold();release(self.users[0],h['id'],1)
        self.assertEqual(Reservation.objects.get(pk=h['id']).status,'cancelled')
        self.make_hold(self.users[1]);self.assertEqual(Reservation.objects.count(),2)

    def test_booked_hold_cannot_be_released(self):
        h=self.make_hold();self.confirm_hold(h)
        with self.assertRaises(BookingError):release(self.users[0],h['id'],2)
        self.slot.refresh_from_db();self.assertIsNotNone(self.slot.active_reservation_id)

    def test_hold_rejected_when_blocked_inactive_or_on_leave(self):
        self.slot.blocked=True;self.slot.save()
        with self.assertRaises(BookingError):self.make_hold()
        self.slot.blocked=False;self.slot.save()
        self.session.active=False;self.session.save()
        with self.assertRaises(BookingError):self.make_hold()
        self.session.active=True;self.session.save()
        Leave.objects.create(facility=self.facility,doctor=self.doctor,starts_on=DAY,ends_on=DAY)
        with self.assertRaises(BookingError):self.make_hold()

    def test_session_disabled_after_hold_prevents_confirmation(self):
        h=self.make_hold();self.session.active=False;self.session.save()
        with self.assertRaises(BookingError):self.confirm_hold(h)
        self.assertEqual(Appointment.objects.count(),0)

    def test_past_and_release_boundary(self):
        self.clock.return_value=self.slot.starts_at
        with self.assertRaises(BookingError):self.make_hold()
        self.clock.return_value=NOW-timedelta(days=29)
        with self.assertRaises(BookingError):self.make_hold()

    def test_unverified_patient_cannot_hold(self):
        self.users[0].email_verified=False;self.users[0].save()
        self.assertEqual(self.post_hold().status_code,403)

    def test_stale_version_and_missing_key(self):
        self.assertEqual(self.post_hold(expected_version=999).status_code,409)
        self.assertEqual(self.post_hold(key='').status_code,400)
        self.assertEqual(Reservation.objects.count(),0)

    def test_hold_idempotency_and_changed_body(self):
        a=self.post_hold();b=self.post_hold()
        self.assertEqual(a.status_code,201);self.assertEqual(a.json()['id'],b.json()['id'])
        self.assertEqual(Reservation.objects.count(),1)
        self.assertEqual(self.post_hold(expected_version=2).status_code,409)

    def test_confirmation_idempotency_and_no_privilege_payload(self):
        h=self.post_hold().json();body={'hold_id':h['id'],'expected_version':1,'payment_method':'counter_due','consent_version':'demo-v1','reason_category':'routine'}
        a=self.api.post('/api/v1/appointments/',body,format='json',HTTP_IDEMPOTENCY_KEY='confirm')
        b=self.api.post('/api/v1/appointments/',body,format='json',HTTP_IDEMPOTENCY_KEY='confirm')
        self.assertEqual(a.status_code,201);self.assertEqual(a.json()['id'],b.json()['id']);self.assertEqual(Appointment.objects.count(),1)
        self.assertEqual(self.api.post('/api/v1/appointments/',{**body,'state':'paid'},format='json',HTTP_IDEMPOTENCY_KEY='forged').status_code,400)

    def test_counter_only_and_consent_required(self):
        h=self.make_hold();body={'hold_id':h['id'],'expected_version':1,'payment_method':'online','consent_version':'demo-v1','reason_category':'routine'}
        self.assertEqual(self.api.post('/api/v1/appointments/',body,format='json',HTTP_IDEMPOTENCY_KEY='x').status_code,400)
        body['payment_method']='counter_due';body.pop('consent_version')
        self.assertEqual(self.api.post('/api/v1/appointments/',body,format='json',HTTP_IDEMPOTENCY_KEY='y').status_code,400)

    def test_other_patient_cannot_read_confirm_or_release(self):
        h=self.make_hold();self.api.force_authenticate(self.users[1])
        self.assertEqual(self.api.get('/api/v1/holds/'+h['id']+'/').status_code,404)
        self.assertEqual(self.api.delete('/api/v1/holds/'+h['id']+'/',{'expected_version':1},format='json',HTTP_IDEMPOTENCY_KEY='release').status_code,404)
        a=self.confirm_hold(h)
        self.assertEqual(self.api.get('/api/v1/appointments/'+a['id']+'/').status_code,404)
        self.assertEqual(self.api.get('/api/v1/appointments/').json()['count'],0)
        self.assertEqual(self.post_hold(patient_id=str(self.patients[0].pk)).status_code,404)

    def test_reception_uses_shared_inventory_and_revocation(self):
        self.api.force_authenticate(self.reception)
        response=self.post_hold(patient_id=str(self.patients[0].pk))
        self.assertEqual(response.status_code,201)
        self.membership.active=False;self.membership.save()
        self.assertEqual(self.api.get('/api/v1/holds/'+response.json()['id']+'/').status_code,404)
        self.api.force_authenticate(self.users[1]);self.assertEqual(self.post_hold(key='competitor').status_code,409)

    def test_staff_patient_and_inline_retry(self):
        self.api.force_authenticate(self.reception)
        body={'facility':self.facility.pk,'name':'New Patient'}
        a=self.api.post('/api/v1/patients/',body,format='json',HTTP_IDEMPOTENCY_KEY='create-patient')
        b=self.api.post('/api/v1/patients/',body,format='json',HTTP_IDEMPOTENCY_KEY='create-patient')
        self.assertEqual(a.status_code,201);self.assertEqual(a.json()['id'],b.json()['id'])
        self.assertIsNone(Patient.objects.get(pk=a.json()['id']).user_id)
        h=self.post_hold(patient_id=a.json()['id']).json()
        result=confirm(self.reception,h['id'],1,'demo-v1','new_visit','counter_due')
        self.assertEqual(result['source'],'reception')

    def test_duplicate_patient_names_warn_without_merging(self):
        self.api.force_authenticate(self.reception)
        body={'facility':self.facility.pk,'name':self.patients[0].name}
        response=self.api.post('/api/v1/patients/',body,format='json',HTTP_IDEMPOTENCY_KEY='same-name')
        self.assertEqual(response.status_code,201);self.assertTrue(response.json()['possible_duplicate'])
        self.assertNotEqual(response.json()['id'],str(self.patients[0].pk))

    def test_availability_scope_and_no_patient_information(self):
        url=f'/api/v1/availability/?facility_id={self.facility.pk}&date={DAY}'
        self.make_hold();data=self.api.get(url).json()
        self.assertEqual(data['count'],11);self.assertEqual(data['results'][0]['status'],'held')
        self.assertNotIn('patient_id',str(data));self.assertNotIn('patient_name',str(data));self.assertIn('generated_at',data)
        self.assertEqual(self.api.get(f'/api/v1/availability/?facility_id={self.other.pk}&date={DAY}').status_code,404)
        self.assertEqual(self.api.get(url+'&date=invalid').status_code,400)

    def test_earliest_availability_skips_occupied_slots(self):
        self.make_hold()
        data=self.api.get(f'/api/v1/availability/?facility_id={self.facility.pk}').json()
        self.assertEqual(data['count'],10)
        self.assertEqual(data['results'][0]['local_date'],str(DAY))
        self.assertTrue(all(s['status']=='available' for s in data['results']))
        self.assertNotIn(str(self.slot.pk),[s['id'] for s in data['results']])

    def test_reception_reference_lookup(self):
        a=self.confirm_hold(self.make_hold());self.api.force_authenticate(self.reception)
        data=self.api.get('/api/v1/appointments/?q='+a['reference']).json()
        self.assertEqual(data['count'],1);self.assertEqual(data['results'][0]['reference'],a['reference'])
        self.assertEqual(self.api.get('/api/v1/appointments/?q=SC-MISSING').json()['count'],0)

    def test_doctor_read_only_own_appointments(self):
        a=self.confirm_hold(self.make_hold());self.api.force_authenticate(self.doctor_user)
        self.assertEqual(self.api.get('/api/v1/appointments/').json()['count'],1)
        self.assertEqual(self.post_hold().status_code,403)
        another=User.objects.create_user('otherdoc@example.test',role='doctor');Membership.objects.create(user=another,facility=self.facility)
        self.api.force_authenticate(another);self.assertEqual(self.api.get('/api/v1/appointments/'+a['id']+'/').status_code,404)

    def test_confirmation_rollback_preserves_hold(self):
        h=self.make_hold()
        with patch('appointments.services.enqueue', side_effect=RuntimeError('outbox failure')):
            with self.assertRaises(RuntimeError):self.confirm_hold(h)
        self.assertEqual(Appointment.objects.count(),0);self.assertEqual(AppointmentHistory.objects.count(),0)
        self.assertEqual(Reservation.objects.get(pk=h['id']).status,'held')

    def test_html_pages_and_scope(self):
        self.client.force_login(self.users[0]);self.assertEqual(self.client.get('/book/').status_code,200)
        a=self.confirm_hold(self.make_hold());self.assertContains(self.client.get('/appointments/'),a['reference'])
        self.assertContains(self.client.get('/appointments/'+a['id']+'/'),'2500.00')
        self.assertEqual(self.client.get('/schedule/publish/').status_code,403)
        self.client.force_login(self.users[1]);self.assertEqual(self.client.get('/appointments/'+a['id']+'/').status_code,404)
        self.client.force_login(self.admin);self.assertEqual(self.client.get('/schedule/publish/').status_code,200)
        self.assertEqual(self.client.post('/schedule/publish/',{'facility':self.other.pk,'start_date':str(DAY),'days':1}).status_code,200)
        self.assertEqual(Session.objects.count(),1)

    def test_csrf_required(self):
        client=APIClient(enforce_csrf_checks=True);client.force_login(self.users[0])
        self.assertEqual(self.post_hold(client=client).status_code,403)


@tag('phase7')
@override_settings(DEBUG=True, CACHES={'default':{'BACKEND':'django.core.cache.backends.locmem.LocMemCache'}})
class BookingConcurrencyTests(BookingFixture, TransactionTestCase):
    def race(self, functions):
        barrier=Barrier(len(functions))
        def run(fn):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return fn()
            except BookingError as exc:return exc.code
            finally:connection.close()
        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            return list(pool.map(run,functions))

    def test_two_patients_last_slot_exactly_one_wins(self):
        def attempt(user_id):
            user=User.objects.get(pk=user_id)
            h=hold(user,self.slot.pk,1)
            return confirm(user,h['id'],1,'demo-v1','new_visit','counter_due')['reference']
        results=self.race([lambda:attempt(self.users[0].pk),lambda:attempt(self.users[1].pk)])
        self.assertEqual(Appointment.objects.count(),1);self.assertEqual(Reservation.objects.count(),1)
        self.assertEqual(sum(r.startswith('SC-') for r in results),1)

    def test_reception_vs_patient_shared_slot(self):
        def reception():return hold(User.objects.get(pk=self.reception.pk),self.slot.pk,1,self.patients[0].pk)
        results=self.race([reception,lambda:hold(User.objects.get(pk=self.users[1].pk),self.slot.pk,1)])
        self.assertEqual(sum(isinstance(r,dict) for r in results),1);self.assertEqual(Reservation.objects.count(),1)

    def test_duplicate_confirmation_command_has_one_effect(self):
        h=self.make_hold()
        def submit():
            user=User.objects.get(pk=self.users[0].pk)
            return command(user,'appointment.confirm','same-key',{'hold_id':h['id']},lambda:confirm(user,h['id'],1,'demo-v1','routine','counter_due'))
        results=self.race([submit,submit])
        self.assertEqual(results[0]['id'],results[1]['id']);self.assertEqual(Appointment.objects.count(),1)
        self.assertEqual(AppointmentHistory.objects.count(),1);self.assertEqual(OutboxEvent.objects.filter(topic='appointment.confirmed').count(),1)

    def test_expiry_worker_racing_confirmation_at_boundary(self):
        h=self.make_hold();self.clock.return_value=datetime.fromisoformat(h['expires_at'])
        results=self.race([expire_holds,lambda:self.confirm_hold(h)])
        self.assertIn('hold_expired',results);self.assertEqual(Appointment.objects.count(),0)
        self.slot.refresh_from_db();self.assertIsNone(self.slot.active_reservation_id)

    def test_concurrent_session_generation_is_repeatable(self):
        results=self.race([lambda:generate(self.rule.pk,DAY+timedelta(days=7)),lambda:generate(self.rule.pk,DAY+timedelta(days=7))])
        self.assertEqual(sum(r[1] for r in results),1)
        self.assertEqual(Session.objects.filter(date=DAY+timedelta(days=7)).count(),1)

    def test_confirmation_racing_release_preserves_consistent_inventory(self):
        h=self.make_hold()
        results=self.race([lambda:self.confirm_hold(h),lambda:release(self.users[0],h['id'],1)])
        self.slot.refresh_from_db()
        reservation=Reservation.objects.get(pk=h['id'])
        if Appointment.objects.exists():
            self.assertEqual(Appointment.objects.count(),1)
            self.assertEqual(reservation.status,'booked')
            self.assertEqual(self.slot.active_reservation_id,reservation.pk)
        else:
            self.assertEqual(reservation.status,'cancelled')
            self.assertIsNone(self.slot.active_reservation_id)
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)

    def test_different_confirmation_keys_still_create_one_appointment(self):
        h=self.make_hold()
        def submit(key):
            user=User.objects.get(pk=self.users[0].pk)
            return command(user,'appointment.confirm',key,{'hold_id':h['id']},lambda:confirm(user,h['id'],1,'demo-v1','routine','counter_due'))
        results=self.race([lambda:submit('first'),lambda:submit('second')])
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)
        self.assertEqual(Appointment.objects.count(),1)
        self.assertEqual(AppointmentHistory.objects.count(),1)
