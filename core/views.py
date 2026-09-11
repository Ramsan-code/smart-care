from datetime import date
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import connection
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView
from rest_framework.response import Response
from accounts.models import Membership, Patient
from accounts.permissions import facility_ids, require_role
from configuration.models import Facility, Doctor, Service, ScheduleRule, FeeVersion, PolicyVersion, MessageTemplate
from .models import AuditEvent, OutboxEvent
from .services import audit


def home(request):
    facilities=Facility.objects.filter(active=True,accepts_registration=True)
    doctors=Doctor.objects.filter(facility__in=facilities,active=True,verified=True).select_related('specialty','facility')
    return render(request,'home.html',{'doctors':doctors,'facility_count':facilities.count()})


@login_required
def workspace(request):
    if request.user.role=='patient':
        ids=[request.user.patient.facility_id]
    else:
        require_role(request.user,['administrator','reception','doctor','finance'])
        ids=facility_ids(request.user)
    doctors=Doctor.objects.filter(facility_id__in=ids,active=True).select_related('specialty','facility')
    rules=ScheduleRule.objects.filter(facility_id__in=ids).select_related('doctor','service')
    if request.user.role=='doctor': rules=rules.filter(doctor__user=request.user)
    today=date.today()
    fees=FeeVersion.objects.filter(facility_id__in=ids,effective_from__lte=today).filter(Q(effective_until__isnull=True)|Q(effective_until__gt=today)).select_related('service')
    events=AuditEvent.objects.filter(facility_id__in=ids).select_related('actor').order_by('-created_at')[:5] if request.user.role=='administrator' else []
    return render(request,'workspace.html',{'doctors':doctors,'rules':rules[:8],'fees':fees,'events':events,
        'doctor_count':doctors.count(),'service_count':Service.objects.filter(facility_id__in=ids,active=True).count(),
        'rule_count':rules.count(),'member_count':Membership.objects.filter(facility_id__in=ids,active=True).count(),
        'policy':PolicyVersion.objects.filter(facility_id__in=ids).order_by('-effective_from').first()})


@login_required
def configuration_overview(request):
    require_role(request.user,['administrator'])
    ids=facility_ids(request.user)
    models=[('Facilities',Facility.objects.filter(pk__in=ids),'facility'),('Doctors',Doctor.objects.filter(facility_id__in=ids),'doctor'),
            ('Services',Service.objects.filter(facility_id__in=ids),'service'),('Working hours',ScheduleRule.objects.filter(facility_id__in=ids),'schedulerule'),
            ('Fee versions',FeeVersion.objects.filter(facility_id__in=ids),'feeversion'),('Booking policies',PolicyVersion.objects.filter(facility_id__in=ids),'policyversion'),
            ('Message templates',MessageTemplate.objects.filter(facility_id__in=ids),'messagetemplate')]
    sections=[{'name':name,'count':qs.count(),'slug':slug,'items':qs[:5]} for name,qs,slug in models]
    return render(request,'configuration.html',{'sections':sections})


@login_required
def audit_log(request):
    require_role(request.user,['administrator'])
    events=AuditEvent.objects.filter(facility_id__in=facility_ids(request.user)).select_related('actor','facility').order_by('-created_at')[:100]
    return render(request,'audit.html',{'events':events})


class CatalogView(APIView):
    permission_classes=[AllowAny]
    def get(self,request):
        key='catalog:v1'
        try: data=cache.get(key)
        except Exception: data=None
        if data is None:
            facilities=Facility.objects.filter(active=True,accepts_registration=True)
            data={'facilities':list(facilities.values('id','name','timezone','currency')),
                  'doctors':list(Doctor.objects.filter(facility__in=facilities,active=True,verified=True).values('id','name','specialty__name','facility_id')),
                  'services':list(Service.objects.filter(facility__in=facilities,active=True).values('id','name','duration_minutes')),
                  'booking_available':False,'phase':2}
            try: cache.set(key,data,30)
            except Exception: pass
        return Response(data)


@login_required
def health(request):
    require_role(request.user,['administrator'])
    database='unavailable'; redis='unavailable'
    try:
        with connection.cursor() as cursor: cursor.execute('SELECT 1'); cursor.fetchone()
        database='connected'
    except Exception: pass
    try: cache.set('health','ok',10); redis='connected' if cache.get('health')=='ok' else 'unavailable'
    except Exception: pass
    return JsonResponse({'database':database,'redis':redis,'outbox_pending':OutboxEvent.objects.filter(processed_at__isnull=True).count() if database=='connected' else None},status=200 if database==redis=='connected' else 503)
