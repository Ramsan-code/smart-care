from datetime import timedelta
from zoneinfo import ZoneInfo
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from accounts.permissions import require_role, facility_ids
from configuration.models import Facility, Doctor, Service, Specialty, ScheduleRule
from appointments.api import scoped_appointments
from core.services import audit
from .api import booking_facilities
from .models import Slot
from .services import generate, BookingError


@login_required
def book(request):
    require_role(request.user, ['patient', 'reception'])
    facilities = booking_facilities(request.user)
    first = facilities.first()
    today = timezone.now().astimezone(ZoneInfo(first.timezone if first else 'Asia/Colombo')).date()
    next_slot = Slot.objects.filter(session__facility__in=facilities, session__active=True, starts_at__gt=timezone.now()).order_by('starts_at').first()
    data = {'facilities': list(facilities.values('id', 'name', 'timezone')),
            'doctors': list(Doctor.objects.filter(facility__in=facilities, active=True, verified=True).values('id', 'name', 'facility_id', 'specialty_id')),
            'services': list(Service.objects.filter(facility__in=facilities, active=True).values('id', 'name', 'facility_id')),
            'specialties': list(Specialty.objects.filter(facility__in=facilities).values('id', 'name', 'facility_id')),
            'role': request.user.role, 'verified': request.user.email_verified,
            'date': str(next_slot.session.date if next_slot else today), 'today': str(today)}
    return render(request, 'booking.html', {'booking_data': data})


@login_required
def history(request):
    require_role(request.user, ['patient', 'reception', 'doctor', 'administrator'])
    qs = scoped_appointments(request.user)
    query = request.GET.get('q', '')[:40]
    if query: qs = qs.filter(reference__icontains=query)
    page = Paginator(qs, 25).get_page(request.GET.get('page'))
    audit(request.user, 'appointments.read', 'history', detail={'count': page.paginator.count}, correlation_id=request.correlation_id)
    return render(request, 'appointments.html', {'page': page, 'query': query})


@login_required
def detail(request, pk):
    appointment = get_object_or_404(scoped_appointments(request.user), pk=pk)
    audit(request.user, 'appointment.read', pk, appointment.facility, correlation_id=request.correlation_id)
    from finance.services import appointment_financials
    return render(request, 'appointment_detail.html', {'appointment': appointment,
        'financials': appointment_financials(appointment)})


class PublishForm(forms.Form):
    facility = forms.ModelChoiceField(queryset=Facility.objects.none())
    start_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    days = forms.IntegerField(min_value=1, max_value=60, initial=30)
    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['facility'].queryset = Facility.objects.filter(active=True, pk__in=facility_ids(user))
        for field in self.fields.values(): field.widget.attrs['class'] = 'form-control'


@login_required
def publish(request):
    require_role(request.user, ['administrator'])
    form = PublishForm(request.user, request.POST or None, initial={'start_date': timezone.localdate()})
    if request.method == 'POST' and form.is_valid():
        try:
            count = 0
            with transaction.atomic():
                rules = list(ScheduleRule.objects.filter(facility=form.cleaned_data['facility']).order_by('doctor_id', 'id'))
                # Acquire every doctor first so bulk generation respects the global lock order.
                list(Doctor.objects.select_for_update().filter(pk__in=[r.doctor_id for r in rules]).order_by('pk'))
                for offset in range(form.cleaned_data['days']):
                    day = form.cleaned_data['start_date'] + timedelta(days=offset)
                    for rule in rules:
                        _, created = generate(rule.pk, day, request.user)
                        count += created
            messages.success(request, f'Published {count} sessions. Existing sessions and bookings were preserved.')
            return redirect('publish-schedule')
        except (BookingError, ValidationError) as exc:
            form.add_error(None, str(exc))
    return render(request, 'publish_schedule.html', {'form': form})
