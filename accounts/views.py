import hashlib
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, PasswordResetConfirmView
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import transaction, IntegrityError
from django.http import Http404
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST
from .forms import LoginForm, RegistrationForm, ProfileForm, StaffForm
from .models import User, DemoEmail, Membership
from .permissions import require_role, facility_ids
from .services import register_patient, provision_staff
from core.services import audit


class SignInView(LoginView):
    template_name='registration/login.html'
    authentication_form=LoginForm
    def post(self,request,*args,**kwargs):
        key='login:'+hashlib.sha256((request.META.get('REMOTE_ADDR','')+'|'+request.POST.get('username','').lower()).encode()).hexdigest()
        try:
            cache.add(key,0,300)
            attempts=cache.incr(key)
        except Exception:
            return render(request,'registration/unavailable.html',status=503)
        if attempts>10: return render(request,'registration/limited.html',status=429)
        return super().post(request,*args,**kwargs)
    def form_valid(self,form):
        response=super().form_valid(form)
        audit(self.request.user,'auth.login','session',correlation_id=self.request.correlation_id)
        return response


def register(request):
    if request.user.is_authenticated: return redirect('workspace')
    form=RegistrationForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        try: user=register_patient(form,request)
        except IntegrityError: form.add_error('email','This email already has an account.')
        else:
            login(request,user)
            messages.success(request,'Account created. Verify your email in your demo inbox before booking becomes available.')
            return redirect('profile')
    return render(request,'registration/register.html',{'form':form})


@login_required
def verify_email(request,token):
    try: data=signing.loads(token,salt='verify-email',max_age=86400)
    except signing.BadSignature:
        messages.error(request,'This verification link is invalid or has expired.'); return redirect('profile')
    if data['user_id']!=request.user.pk or data['email']!=request.user.email: raise PermissionDenied
    if request.method=='POST':
        with transaction.atomic():
            user=User.objects.select_for_update().get(pk=request.user.pk)
            user.email_verified=True; user.save(update_fields=['email_verified'])
            audit(user,'email.verified',user.pk)
        messages.success(request,'Your email is verified.'); return redirect('profile')
    return render(request,'registration/verify.html')


class ResetConfirmView(PasswordResetConfirmView):
    def form_valid(self,form):
        with transaction.atomic():
            response=super().form_valid(form)
            user=form.user; user.email_verified=True; user.save(update_fields=['email_verified'])
            audit(user,'auth.password_reset',user.pk)
            return response


@login_required
def profile(request):
    require_role(request.user,['patient'])
    patient=request.user.patient
    form=ProfileForm(request.POST or None,instance=patient)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            form.save(); audit(request.user,'profile.updated',patient.pk,patient.facility,{'changed_fields':form.changed_data},request.correlation_id)
        messages.success(request,'Your preferences have been saved.'); return redirect('profile')
    audit(request.user,'patient.read',patient.pk,patient.facility,correlation_id=request.correlation_id)
    return render(request,'profile.html',{'form':form,'patient':patient})


@login_required
def inbox(request):
    if not settings.DEMO_MODE: raise Http404
    mail=DemoEmail.objects.filter(recipient=request.user.email).order_by('-created_at')[:20]
    response=render(request,'inbox.html',{'mail':mail})
    response['Cache-Control']='no-store'
    return response


@login_required
def staff(request):
    require_role(request.user,['administrator'])
    form=StaffForm(request.POST or None,user=request.user)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            user=provision_staff(form,request.user)
            from django.contrib.auth.forms import PasswordResetForm
            reset=PasswordResetForm({'email':user.email})
            if reset.is_valid(): reset.save(request=request,use_https=request.is_secure(),email_template_name='registration/password_reset_email.html')
        messages.success(request,'Staff account created. A password invitation was delivered to the development inbox.'); return redirect('staff')
    memberships=Membership.objects.filter(facility_id__in=facility_ids(request.user)).select_related('user','facility')
    return render(request,'staff.html',{'form':form,'memberships':memberships})


@login_required
@require_POST
def toggle_membership(request,pk):
    require_role(request.user,['administrator'])
    from django.shortcuts import get_object_or_404
    with transaction.atomic():
        membership=get_object_or_404(Membership.objects.select_for_update(),pk=pk,facility_id__in=facility_ids(request.user))
        if membership.user_id==request.user.pk: raise PermissionDenied('You cannot revoke your own access here.')
        membership.active=not membership.active; membership.save(update_fields=['active'])
        audit(request.user,'membership.changed',membership.pk,membership.facility,{'active':membership.active})
    return redirect('staff')
