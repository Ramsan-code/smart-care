from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers, generics
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError
from .models import Patient, Consent
from .permissions import patient_scope, facility_ids
from configuration.models import Facility
from core.services import audit, execute_once


class StrictSerializer(serializers.ModelSerializer):
    def to_internal_value(self,data):
        unknown=set(data)-{name for name,field in self.fields.items() if not field.read_only}
        if unknown: raise ValidationError({key:'This field cannot be set.' for key in unknown})
        return super().to_internal_value(data)


class ProfileSerializer(StrictSerializer):
    class Meta:
        model=Patient
        fields=['id','name','email','phone','date_of_birth','assistance','sms_enabled']
        read_only_fields=['id','name','email']


class MeView(APIView):
    def get(self,request):
        data={'id':request.user.pk,'name':request.user.get_full_name(),'email':request.user.email,'role':request.user.role,'email_verified':request.user.email_verified}
        if request.user.role=='patient':
            patient=request.user.patient
            data['profile']=ProfileSerializer(patient).data
            audit(request.user,'patient.read',patient.pk,patient.facility,correlation_id=request.correlation_id)
        return Response(data)
    def patch(self,request):
        if request.user.role!='patient': raise PermissionDenied
        patient=request.user.patient
        serializer=ProfileSerializer(patient,data=request.data,partial=True); serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            serializer.save(); audit(request.user,'profile.updated',patient.pk,patient.facility,{'changed_fields':list(serializer.validated_data)},request.correlation_id)
        return Response(serializer.data)


class ConsentView(APIView):
    def post(self,request):
        if request.user.role!='patient': raise PermissionDenied
        if set(request.data)!={'version','purpose','accepted'} or request.data.get('version')!='demo-v1' or request.data.get('purpose')!='booking' or type(request.data.get('accepted')) is not bool:
            raise ValidationError('Use version demo-v1, purpose booking and a boolean accepted value.')
        patient=request.user.patient
        def command():
            consent=Consent.objects.create(patient=patient,**request.data)
            audit(request.user,'consent.recorded',consent.pk,patient.facility,{'version':consent.version,'accepted':consent.accepted})
            return {'id':consent.pk,'version':consent.version,'accepted':consent.accepted}
        try: result=execute_once(request.user,'consent',request.headers.get('Idempotency-Key'),dict(request.data),command)
        except DjangoValidationError as exc: return Response({'code':'idempotency_conflict','message':'; '.join(exc.messages)},status=409)
        return Response(result,status=201)


class StaffPatientSerializer(StrictSerializer):
    class Meta:
        model=Patient
        fields=['id','facility','name','email','phone','date_of_birth','assistance']
        read_only_fields=['id']
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['facility'].queryset=Facility.objects.filter(pk__in=facility_ids(self.context['request'].user))


class PatientList(generics.ListCreateAPIView):
    serializer_class=StaffPatientSerializer
    def get_queryset(self):
        if self.request.user.role!='reception': raise PermissionDenied
        qs=patient_scope(self.request.user).order_by('name')
        if q:=self.request.query_params.get('q'):
            from django.db.models import Q
            qs=qs.filter(Q(name__icontains=q)|Q(email__iexact=q))
        return qs
    def list(self,request,*args,**kwargs):
        result=super().list(request,*args,**kwargs)
        for item in result.data['results']:
            item['phone']=('•••• '+item['phone'][-3:]) if item['phone'] else ''
            item['email']=(item['email'][:1]+'•••@'+item['email'].split('@')[-1]) if item['email'] else ''
            item.pop('date_of_birth',None); item.pop('assistance',None)
        audit(request.user,'patients.searched','patients',detail={'count':result.data['count']})
        return result
    def perform_create(self,serializer):
        if self.request.user.role!='reception' or not list(facility_ids(self.request.user)): raise PermissionDenied
        with transaction.atomic():
            patient=serializer.save()
            audit(self.request.user,'patient.created',patient.pk,patient.facility)


class PatientDetail(generics.RetrieveAPIView):
    serializer_class=StaffPatientSerializer
    def get_queryset(self): return patient_scope(self.request.user)
    def retrieve(self,request,*args,**kwargs):
        patient=self.get_object()
        audit(request.user,'patient.read',patient.pk,patient.facility,correlation_id=request.correlation_id)
        return Response(self.get_serializer(patient).data)
