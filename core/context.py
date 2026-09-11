from django.conf import settings
from accounts.permissions import facility_ids
from configuration.models import Facility

def app_context(request):
    facility=None
    if request.user.is_authenticated:
        if request.user.role == 'patient' and hasattr(request.user,'patient'):
            facility=request.user.patient.facility
        else: facility=Facility.objects.filter(pk__in=facility_ids(request.user)).first()
    return {'demo_mode':settings.DEMO_MODE,'current_facility':facility}
