import secrets
from datetime import date, time
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from accounts.models import User, Membership, Patient, Consent
from accounts.services import assign_role
from configuration.models import Organization, Facility, Specialty, Department, Room, Doctor, Service, FeeVersion, PolicyVersion, ScheduleRule, Leave, MessageTemplate
from core.services import audit, enqueue


class Command(BaseCommand):
    help='Load fictional demo configuration and generate local credentials; existing data is preserved.'
    def add_arguments(self,parser):
        parser.add_argument('--base-date',default='2026-09-11')
    @transaction.atomic
    def handle(self,*args,**options):
        if not settings.DEMO_MODE or not settings.DEBUG: raise CommandError('Demo seeding requires DEMO_MODE=true and DJANGO_DEBUG=true.')
        base=date.fromisoformat(options['base_date'])
        org,_=Organization.objects.get_or_create(code='DEMO-ORG',defaults={'name':'Smart Care Demonstration'})
        facility,_=Facility.objects.get_or_create(code='DEMO-MAIN',defaults={'organization':org,'name':'Greenway Demo Clinic','accepts_registration':True})
        other,_=Facility.objects.get_or_create(code='DEMO-OTHER',defaults={'organization':org,'name':'Isolated Demo Facility'})
        external,_=Organization.objects.get_or_create(code='OTHER-ORG',defaults={'name':'Isolated Test Organization'})
        Facility.objects.get_or_create(code='OTHER-FACILITY',defaults={'organization':external,'name':'Other Organization Facility'})
        lines=[]
        def account(email,name,role,site=facility,approver=False,password=None):
            user=User.objects.filter(email=email).first()
            if user is None:
                password=password or secrets.token_urlsafe(15)
                user=User.objects.create_user(email,password,first_name=name,role=role,email_verified=True)
            elif password is not None:
                user.set_password(password)
                user.save(update_fields=['password'])
            if role!='patient': Membership.objects.get_or_create(user=user,facility=site,defaults={'finance_approver':approver})
            if password is not None:
                lines.append(f'{role:15} {email:38} {password}')
            return user
        admin=account('admin@example.test','Alex','administrator',password='Admin123!')
        account('reception@example.test','Nila','reception',password='Reception123!')
        account('finance@example.test','Sam','finance',password='Finance123!')
        account('approver@example.test','Robin','finance',approver=True,password='Finance123!')
        account('isolated@example.test','Isolated Reception','reception',site=other,password='Reception123!')
        specialties=[Specialty.objects.get_or_create(facility=facility,name=n)[0] for n in ['General medicine','Cardiology']]
        Department.objects.get_or_create(facility=facility,name='Outpatient care')
        Room.objects.get_or_create(facility=facility,name='Consultation room 1')
        services=[Service.objects.get_or_create(facility=facility,name=n)[0] for n in ['General consultation','Specialist consultation']]
        for i,service in enumerate(services):
            FeeVersion.objects.get_or_create(facility=facility,service=service,effective_from=base,defaults={'doctor_fee':2000+i*1000,'facility_fee':500})
        PolicyVersion.objects.get_or_create(facility=facility,version='demo-v1',defaults={'effective_from':base})
        for i,name in enumerate(['Maya Perera','Arun Silva','Leena Fernando']):
            user=account(f'doctor{i+1}@example.test',name,'doctor',password=f'Doctor{i+1}123!')
            doctor,_=Doctor.objects.get_or_create(facility=facility,name=name,defaults={'user':user,'specialty':specialties[1 if i==1 else 0],'verified':True})
            for weekday in [0,2,4]:
                ScheduleRule.objects.get_or_create(facility=facility,doctor=doctor,service=services[1 if i==1 else 0],weekday=weekday,effective_from=base,
                    defaults={'starts_at':time(9),'ends_at':time(12),'break_start':time(10),'break_end':time(10,15)})
        for i in range(1,21):
            user=account(f'patient{i:02}@example.test',f'Demo Patient {i:02}','patient',password=f'Patient{i:02}123!')
            patient,_=Patient.objects.get_or_create(user=user,defaults={'facility':facility,'name':f'Demo Patient {i:02}','email':user.email,'phone':f'SIM-{i:03}'})
            if not patient.consents.exists(): Consent.objects.create(patient=patient,version='demo-v1',accepted=True)
        Patient.objects.get_or_create(facility=other,name='Isolated Patient',defaults={'email':'private@example.test','phone':'SIM-OTHER'})
        for event,_ in MessageTemplate._meta.get_field('event').choices:
            MessageTemplate.objects.get_or_create(facility=facility,event=event,language='en',defaults={'body':'Smart Care: {reference} at {facility} on {date} at {time}. View your portal for details.'})
        if lines:
            credentials=settings.BASE_DIR/'demo-credentials.txt'
            with credentials.open('w') as file:
                file.write('\nSmart Care LOCAL DEMO credentials — synthetic accounts only\nROLE            EMAIL                                  PASSWORD\n'+'\n'.join(lines)+'\n')
            credentials.chmod(0o600)
            audit(admin,'demo.seeded','DEMO-MAIN',facility,{'new_accounts':len(lines)})
            enqueue('demo:foundation:ready','demo.ping',{'facility_id':facility.pk})
        self.stdout.write(self.style.SUCCESS(f'Demo ready. {len(lines)} demo credentials updated. Credentials: {settings.BASE_DIR / "demo-credentials.txt"}'))
