from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from accounts.permissions import facility_ids
from accounts.models import User
from core.services import audit, enqueue
from .models import Organization, Facility, Department, Specialty, Room, Doctor, Service, FeeVersion, PolicyVersion, ScheduleRule, Leave, MessageTemplate
import uuid
import json
from django.forms.models import model_to_dict
from django.core.serializers.json import DjangoJSONEncoder


class ScopedAdmin(admin.ModelAdmin):
    list_per_page=25
    actions=None
    def allowed(self,request):
        return request.user.is_active and (request.user.is_superuser or (request.user.role=='administrator' and bool(list(facility_ids(request.user)))))
    def get_queryset(self,request):
        qs=super().get_queryset(request)
        if request.user.is_superuser: return qs
        if not self.allowed(request): return qs.none()
        if self.model is Facility: return qs.filter(pk__in=facility_ids(request.user))
        return qs.filter(facility_id__in=facility_ids(request.user))
    def has_module_permission(self,request): return self.allowed(request)
    def has_view_permission(self,request,obj=None): return self.has_change_permission(request,obj)
    def has_change_permission(self,request,obj=None):
        if not self.allowed(request): return False
        if obj is None or request.user.is_superuser: return True
        facility_id=obj.pk if isinstance(obj,Facility) else obj.facility_id
        return facility_id in facility_ids(request.user)
    def has_add_permission(self,request): return self.allowed(request) and (self.model is not Facility or request.user.is_superuser)
    def has_delete_permission(self,request,obj=None): return False
    def get_readonly_fields(self,request,obj=None):
        fields=list(super().get_readonly_fields(request,obj))
        if obj:
            fields+=['organization','code'] if self.model is Facility else ['facility']
        return fields
    def formfield_for_foreignkey(self,db_field,request,**kwargs):
        related=db_field.remote_field.model
        ids=facility_ids(request.user)
        if related is Facility: kwargs['queryset']=Facility.objects.filter(pk__in=ids)
        elif related is User: kwargs['queryset']=User.objects.filter(role='doctor',memberships__facility_id__in=ids,memberships__active=True).distinct()
        elif any(field.name=='facility' for field in related._meta.fields): kwargs['queryset']=related.objects.filter(facility_id__in=ids)
        return super().formfield_for_foreignkey(db_field,request,**kwargs)
    def save_model(self,request,obj,form,change):
        if not self.has_change_permission(request,obj): raise PermissionDenied
        with transaction.atomic():
            # Serialize all configuration changes per facility before final revalidation.
            facility=obj if isinstance(obj,Facility) else obj.facility
            if facility.pk: Facility.objects.select_for_update().get(pk=facility.pk)
            before=model_to_dict(self.model.objects.get(pk=obj.pk)) if change else None
            obj.full_clean(); obj.save()
            detail=json.loads(json.dumps({'fields':form.changed_data,'before':before,'after':model_to_dict(obj)},cls=DjangoJSONEncoder))
            audit(request.user,'configuration.changed',f'{obj._meta.label}:{obj.pk}',facility,detail,request.correlation_id)
            enqueue(f'config:{uuid.uuid4()}','configuration.changed',{'model':obj._meta.label,'id':obj.pk})
    def change_view(self,request,object_id,form_url='',extra_context=None):
        obj=self.get_object(request,object_id)
        if obj and request.method=='GET':
            audit(request.user,'configuration.read',f'{obj._meta.label}:{obj.pk}',obj if isinstance(obj,Facility) else obj.facility)
        return super().change_view(request,object_id,form_url,extra_context)


class VersionAdmin(ScopedAdmin):
    def get_readonly_fields(self,request,obj=None):
        if obj: return [field.name for field in self.model._meta.fields]
        return super().get_readonly_fields(request,obj)
    def save_model(self,request,obj,form,change):
        if change: raise PermissionDenied('Create a new effective-dated version instead.')
        super().save_model(request,obj,form,change)


for model in [Facility,Department,Specialty,Room,Doctor,Service,ScheduleRule,Leave,MessageTemplate]:
    admin.site.register(model,ScopedAdmin)
for model in [FeeVersion,PolicyVersion]: admin.site.register(model,VersionAdmin)
admin.site.site_header='Smart Care administration'
admin.site.site_title='Smart Care'
admin.site.index_title='Clinic configuration'
