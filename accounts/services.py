import secrets
import uuid
from django.contrib.auth.models import Group, Permission
from django.core import signing
from django.core.mail import send_mail
from django.db import transaction
from .models import User, Patient, Consent, Membership
from core.services import audit, enqueue


def assign_role(user):
    group,_=Group.objects.get_or_create(name=user.get_role_display())
    # Groups supply Django model permission checks; facility scoping remains server-side.
    if user.role=='administrator':
        group.permissions.set(Permission.objects.filter(content_type__app_label='configuration').exclude(codename__startswith='delete_'))
    user.groups.set([group])
    user.is_staff=user.role=='administrator'
    user.save(update_fields=['is_staff'])


@transaction.atomic
def register_patient(form,request):
    user=form.save(commit=False)
    user.role='patient'; user.email_verified=False; user.is_staff=False; user.is_superuser=False
    user.save(); assign_role(user)
    patient=Patient.objects.create(user=user,facility=form.cleaned_data['facility'],name=user.get_full_name(),email=user.email,phone=form.cleaned_data['phone'])
    Consent.objects.create(patient=patient,version='demo-v1',accepted=True)
    audit(user,'account.registered',patient.pk,patient.facility,{'consent_version':'demo-v1'},request.correlation_id)
    enqueue(f'account:{user.pk}:registered','account.registered',{'user_id':user.pk})
    token=signing.dumps({'user_id':user.pk,'email':user.email},salt='verify-email')
    url=request.build_absolute_uri('/accounts/verify/'+token+'/')
    send_mail('Verify your Smart Care email',f'Confirm your email address by opening this link (valid for 24 hours):\n\n{url}\n\nThis is a local academic demo.',None,[user.email])
    return user


@transaction.atomic
def provision_staff(form,actor):
    data=form.cleaned_data
    user=User.objects.create_user(data['email'],secrets.token_urlsafe(24),first_name=data['name'],role=data['role'],email_verified=False)
    # Require a password reset invitation before staff can sign in with a known password.
    membership=Membership(user=user,facility=data['facility'],finance_approver=data['finance_approver'])
    membership.full_clean(); membership.save(); assign_role(user)
    audit(actor,'staff.created',user.pk,data['facility'],{'role':user.role,'finance_approver':membership.finance_approver})
    return user
