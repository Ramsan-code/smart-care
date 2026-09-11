from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from accounts.permissions import facility_ids
from core.services import audit
from scheduling.api import DomainView, StrictInput
from scheduling.services import BookingError
from .models import Appointment
from .services import hold, confirm, release, command, appointment_data, authorized_patient, owned_reservation, reservation_data


class HoldInput(StrictInput):
    slot_id = serializers.UUIDField()
    expected_version = serializers.IntegerField(min_value=1)
    patient_id = serializers.UUIDField(required=False)


class ConfirmInput(StrictInput):
    hold_id = serializers.UUIDField()
    expected_version = serializers.IntegerField(min_value=1)
    patient_id = serializers.UUIDField(required=False)
    payment_method = serializers.ChoiceField(choices=['counter_due'])
    consent_version = serializers.ChoiceField(choices=['demo-v1'])
    reason_category = serializers.ChoiceField(choices=['new_visit', 'follow_up', 'routine'])


class VersionInput(StrictInput):
    expected_version = serializers.IntegerField(min_value=1)


def scoped_appointments(user):
    qs = Appointment.objects.select_related('patient', 'facility', 'doctor', 'service')
    if user.role == 'patient': return qs.filter(patient__user=user)
    if user.role in ['reception', 'administrator', 'doctor']:
        qs = qs.filter(facility_id__in=facility_ids(user))
        return qs.filter(doctor__user=user) if user.role == 'doctor' else qs
    return qs.none()


class HoldsView(DomainView):
    def post(self, request):
        form = HoldInput(data=request.data); form.is_valid(raise_exception=True)
        # Recheck scope even when an idempotency response already exists.
        from scheduling.models import Slot
        slot = Slot.objects.filter(pk=form.validated_data['slot_id']).select_related('session').first()
        if not slot: raise BookingError('not_found', 'Appointment time not found.', 404)
        authorized_patient(request.user, form.validated_data.get('patient_id'), slot.session.facility_id)
        result = command(request.user, 'hold.create', request.headers.get('Idempotency-Key'), dict(request.data),
                         lambda: hold(request.user, **form.validated_data))
        return Response({**result, 'correlation_id': str(request.correlation_id)}, status=201)


class HoldDetailView(DomainView):
    def get(self, request, pk):
        return Response(reservation_data(owned_reservation(request.user, pk)))
    def delete(self, request, pk):
        form = VersionInput(data=request.data); form.is_valid(raise_exception=True)
        owned_reservation(request.user, pk)
        result = command(request.user, 'hold.release', request.headers.get('Idempotency-Key'), {'id': str(pk), **dict(request.data)},
                         lambda: release(request.user, pk, **form.validated_data))
        return Response({**result, 'correlation_id': str(request.correlation_id)})


class AppointmentsView(DomainView):
    def post(self, request):
        form = ConfirmInput(data=request.data); form.is_valid(raise_exception=True)
        owned_reservation(request.user, form.validated_data['hold_id'])
        result = command(request.user, 'appointment.confirm', request.headers.get('Idempotency-Key'), dict(request.data),
                         lambda: confirm(request.user, **form.validated_data))
        return Response({**result, 'correlation_id': str(request.correlation_id)}, status=201)
    def get(self, request):
        page_form = serializers.IntegerField(min_value=1)
        page = page_form.run_validation(request.query_params.get('page', 1))
        qs = scoped_appointments(request.user)
        if q := request.query_params.get('q'): qs = qs.filter(reference__icontains=q[:40])
        count = qs.count(); offset = (page - 1) * 25
        def page_url(number):
            query = request.query_params.copy(); query['page'] = str(number)
            return request.path + '?' + query.urlencode()
        audit(request.user, 'appointments.read', 'appointments', detail={'count': count}, correlation_id=request.correlation_id)
        return Response({'count': count, 'next': page_url(page + 1) if offset + 25 < count else None,
                         'previous': page_url(page - 1) if page > 1 else None, 'generated_at': timezone.now().isoformat(),
                         'results': [appointment_data(a) for a in qs[offset:offset + 25]]})


class AppointmentDetailView(DomainView):
    def get(self, request, pk):
        appointment = scoped_appointments(request.user).filter(pk=pk).first()
        if not appointment: raise BookingError('not_found', 'Appointment not found.', 404)
        audit(request.user, 'appointment.read', pk, appointment.facility, correlation_id=request.correlation_id)
        return Response({**appointment_data(appointment), 'history': [
            {'state': h.to_state, 'source': h.source, 'version': h.version, 'at': h.created_at.isoformat()}
            for h in appointment.history.order_by('created_at', 'pk')]})
