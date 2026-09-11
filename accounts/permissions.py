from django.core.exceptions import PermissionDenied
from configuration.models import Facility


def facility_ids(user):
    if not user.is_authenticated or not user.is_active: return []
    if user.is_superuser: return Facility.objects.values_list('id', flat=True)
    return user.memberships.filter(active=True, facility__active=True).values_list('facility_id',flat=True)


def require_role(user, roles):
    if not user.is_authenticated or not user.is_active: raise PermissionDenied
    if user.is_superuser: return
    if user.role not in roles or (user.role != 'patient' and not user.memberships.filter(active=True,facility__active=True).exists()):
        raise PermissionDenied('Your account does not have access to this workspace.')


def patient_scope(user):
    from .models import Patient
    if user.role == 'patient': return Patient.objects.filter(user=user)
    if user.role == 'reception': return Patient.objects.filter(facility_id__in=facility_ids(user))
    return Patient.objects.none()
