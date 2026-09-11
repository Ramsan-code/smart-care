import re
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.test import TestCase, Client, override_settings
from rest_framework.test import APIClient
from configuration.models import Organization, Facility
from .models import Patient, Membership, Consent, DemoEmail
from .services import assign_role

User=get_user_model()

@override_settings(CACHES={'default':{'BACKEND':'django.core.cache.backends.locmem.LocMemCache'}},DEMO_MODE=True,DEBUG=True,EMAIL_BACKEND='accounts.email_backend.DatabaseEmailBackend')
class AccountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        org=Organization.objects.create(code='A',name='Clinic A')
        other=Organization.objects.create(code='B',name='Clinic B')
        cls.facility=Facility.objects.create(organization=org,code='A',name='Main',accepts_registration=True)
        cls.other=Facility.objects.create(organization=other,code='B',name='Private')
        cls.users={}
        for role in ['patient','reception','doctor','finance','administrator']:
            user=User.objects.create_user(role+'@example.test','UnitTest-strong-927!',role=role,first_name=role,email_verified=True)
            cls.users[role]=user; assign_role(user)
            if role!='patient': Membership.objects.create(user=user,facility=cls.facility)
        cls.patient=Patient.objects.create(user=cls.users['patient'],facility=cls.facility,name='Own Patient',email='patient@example.test',phone='SIM-001')
        cls.other_patient=Patient.objects.create(facility=cls.other,name='Private patient',email='hidden@example.test')
    def setUp(self): cache.clear(); self.api=APIClient()
    def test_home_is_public_and_honest(self):
        response=self.client.get('/')
        self.assertContains(response,'A calmer day')
        self.assertContains(response,'Booking comes in Phase 3')
    def test_unauthenticated_api_is_401(self): self.assertEqual(self.api.get('/api/v1/me/').status_code,401)
    def test_all_role_workspaces_render(self):
        for role,user in self.users.items():
            with self.subTest(role=role):
                self.client.force_login(user); self.assertEqual(self.client.get('/workspace/').status_code,200)
    def test_patient_cannot_read_other_patient(self):
        self.api.force_authenticate(self.users['patient'])
        self.assertEqual(self.api.get(f'/api/v1/patients/{self.other_patient.pk}/').status_code,404)
        self.assertEqual(self.api.get(f'/api/v1/patients/{self.patient.pk}/').status_code,200)
    def test_patient_cannot_list_patients(self):
        self.api.force_authenticate(self.users['patient']);self.assertEqual(self.api.get('/api/v1/patients/').status_code,403)
    def test_reception_list_scoped_and_masked(self):
        self.api.force_authenticate(self.users['reception']);data=self.api.get('/api/v1/patients/').json()
        self.assertEqual(data['count'],1);self.assertNotEqual(data['results'][0]['phone'],'SIM-001')
        self.assertNotIn('assistance',data['results'][0])
    def test_foreign_facility_create_denied(self):
        self.api.force_authenticate(self.users['reception'])
        response=self.api.post('/api/v1/patients/',{'facility':self.other.pk,'name':'Forged'},format='json')
        self.assertEqual(response.status_code,400);self.assertFalse(Patient.objects.filter(name='Forged').exists())
    def test_reception_creates_own_facility_patient(self):
        self.api.force_authenticate(self.users['reception'])
        response=self.api.post('/api/v1/patients/',{'facility':self.facility.pk,'name':'Assisted Patient'},format='json')
        self.assertEqual(response.status_code,201)
    def test_finance_and_doctor_cannot_read_patient_registry(self):
        for role in ['finance','doctor','administrator']:
            self.api.force_authenticate(self.users[role])
            self.assertEqual(self.api.get(f'/api/v1/patients/{self.patient.pk}/').status_code,404)
    def test_profile_rejects_privilege_fields(self):
        self.api.force_authenticate(self.users['patient'])
        response=self.api.patch('/api/v1/me/',{'role':'administrator','facility':self.other.pk},format='json')
        self.assertEqual(response.status_code,400)
        self.users['patient'].refresh_from_db();self.assertEqual(self.users['patient'].role,'patient')
    def test_profile_updates_preferences(self):
        self.api.force_authenticate(self.users['patient'])
        self.assertEqual(self.api.patch('/api/v1/me/',{'sms_enabled':False,'assistance':'mobility'},format='json').status_code,200)
        self.patient.refresh_from_db();self.assertFalse(self.patient.sms_enabled)
    def test_revocation_applies_mid_session(self):
        self.client.force_login(self.users['reception'])
        Membership.objects.filter(user=self.users['reception']).update(active=False)
        self.assertEqual(self.client.get('/workspace/').status_code,403)
        self.assertEqual(self.client.get('/api/v1/patients/').json()['count'],0)
    def test_nonadmins_denied_admin_workspace(self):
        for role in ['patient','doctor','reception','finance']:
            self.client.force_login(self.users[role])
            for path in ['/configuration/','/team/','/audit/']:
                self.assertEqual(self.client.get(path).status_code,403,(role,path))
    def test_admin_queryset_and_object_scope(self):
        self.client.force_login(self.users['administrator'])
        response=self.client.get('/admin/configuration/facility/')
        self.assertContains(response,'Main');self.assertNotContains(response,'Private')
        response=self.client.get(f'/admin/configuration/facility/{self.other.pk}/change/')
        self.assertEqual(response.status_code,302)
    def test_admin_cannot_grant_foreign_facility_membership(self):
        self.client.force_login(self.users['administrator'])
        response=self.client.post('/team/',{'name':'New','email':'new@example.test','role':'finance','facility':self.other.pk})
        self.assertEqual(response.status_code,200);self.assertFalse(User.objects.filter(email='new@example.test').exists())
    def test_admin_provisions_local_staff_and_invitation(self):
        self.client.force_login(self.users['administrator'])
        response=self.client.post('/team/',{'name':'New Finance','email':'new@example.test','role':'finance','facility':self.facility.pk,'finance_approver':True})
        self.assertEqual(response.status_code,302)
        user=User.objects.get(email='new@example.test')
        self.assertFalse(user.is_superuser);self.assertTrue(user.memberships.get().finance_approver)
        self.assertTrue(DemoEmail.objects.filter(recipient=user.email).exists())
    def test_registration_ignores_injected_role_and_verifies_contact(self):
        response=self.client.post('/accounts/register/',{'first_name':'New','last_name':'Patient','email':'NEW@EXAMPLE.TEST','facility':self.facility.pk,'phone':'SIM-030','consent':True,'password1':'Registration-strong-591!','password2':'Registration-strong-591!','role':'administrator','is_superuser':True})
        self.assertEqual(response.status_code,302)
        user=User.objects.get(email='new@example.test');self.assertEqual(user.role,'patient');self.assertFalse(user.is_superuser);self.assertFalse(user.email_verified)
        mail=DemoEmail.objects.get(recipient=user.email)
        path=re.search(r'http://testserver([^\s]+)',mail.body).group(1)
        self.assertEqual(self.client.get(path).status_code,200);self.assertFalse(User.objects.get(pk=user.pk).email_verified)
        self.assertEqual(self.client.post(path).status_code,302);self.assertTrue(User.objects.get(pk=user.pk).email_verified)
        self.assertTrue(Consent.objects.filter(patient__user=user,version='demo-v1').exists())
    def test_duplicate_email_normalization(self):
        response=self.client.post('/accounts/register/',{'first_name':'Other','last_name':'Patient','email':'PATIENT@EXAMPLE.TEST','facility':self.facility.pk,'consent':True,'password1':'Registration-strong-591!','password2':'Registration-strong-591!'})
        self.assertEqual(response.status_code,200);self.assertEqual(User.objects.filter(email='patient@example.test').count(),1)
    def test_verification_token_cannot_verify_another_user(self):
        self.client.force_login(self.users['patient'])
        token=signing.dumps({'user_id':self.users['doctor'].pk,'email':self.users['doctor'].email},salt='verify-email')
        self.assertEqual(self.client.post('/accounts/verify/'+token+'/').status_code,403)
    def test_inbox_is_own_only(self):
        DemoEmail.objects.create(recipient='patient@example.test',subject='Own message',body='Own token')
        DemoEmail.objects.create(recipient='doctor@example.test',subject='Private message',body='Private token')
        self.client.force_login(self.users['patient'])
        response=self.client.get('/demo/inbox/');self.assertContains(response,'Own message');self.assertNotContains(response,'Private token')
    def test_inbox_disabled_outside_demo(self):
        self.client.force_login(self.users['patient'])
        with override_settings(DEMO_MODE=False): self.assertEqual(self.client.get('/demo/inbox/').status_code,404)
    def test_consent_is_idempotent_and_rejects_conflicting_replay(self):
        self.api.force_authenticate(self.users['patient'])
        data={'version':'demo-v1','purpose':'booking','accepted':True}
        first=self.api.post('/api/v1/me/consents/',data,format='json',HTTP_IDEMPOTENCY_KEY='same')
        second=self.api.post('/api/v1/me/consents/',data,format='json',HTTP_IDEMPOTENCY_KEY='same')
        self.assertEqual(first.json(),second.json());self.assertEqual(Consent.objects.count(),1)
        data['accepted']=False
        self.assertEqual(self.api.post('/api/v1/me/consents/',data,format='json',HTTP_IDEMPOTENCY_KEY='same').status_code,409)
    def test_csrf_protects_profile_and_login(self):
        client=Client(enforce_csrf_checks=True)
        self.assertEqual(client.post('/accounts/login/',{'username':'patient@example.test','password':'UnitTest-strong-927!'}).status_code,403)
        client.force_login(self.users['patient'])
        self.assertEqual(client.post('/patient/profile/',{'phone':'bad'}).status_code,403)
    def test_sign_in_throttling(self):
        for _ in range(10): self.client.post('/accounts/login/',{'username':'wrong@example.test','password':'wrong'})
        self.assertEqual(self.client.post('/accounts/login/',{'username':'wrong@example.test','password':'wrong'}).status_code,429)
    def test_password_reset_token_single_use(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.http import urlsafe_base64_encode
        from django.utils.encoding import force_bytes
        user=self.users['patient'];token=default_token_generator.make_token(user)
        path=f'/accounts/reset/{urlsafe_base64_encode(force_bytes(user.pk))}/{token}/'
        response=self.client.get(path);self.assertEqual(response.status_code,302)
        response=self.client.post(response.url,{'new_password1':'Reset-strong-824!','new_password2':'Reset-strong-824!'})
        self.assertEqual(response.status_code,302)
        user.refresh_from_db();self.assertTrue(user.check_password('Reset-strong-824!'));self.assertFalse(default_token_generator.check_token(user,token))
    def test_public_catalog_has_no_private_data(self):
        response=self.client.get('/api/v1/catalog/')
        self.assertEqual(response.status_code,200);self.assertNotContains(response,'Private')
        self.assertNotContains(response,'patient@example.test')
