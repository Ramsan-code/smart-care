from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework.views import APIView
from rest_framework.response import Response
from accounts.permissions import facility_ids
from configuration.models import Facility
from core.services import audit
from .models import Slot
from .services import BookingError, eligible


class DomainView(APIView):
    def handle_exception(self, exc):
        if isinstance(exc, BookingError):
            if exc.code in ['capacity_conflict', 'stale_version', 'state_conflict']:
                from operations.telemetry import count
                count('booking_conflicts')
            return Response({'code': exc.code, 'message': exc.message, 'field_errors': {},
                             'correlation_id': str(getattr(self.request, 'correlation_id', ''))}, status=exc.status)
        response = super().handle_exception(exc)
        if response is not None:
            response.data = {'code': 'validation' if response.status_code == 400 else 'access_error',
                             'message': 'Check the highlighted fields.' if response.status_code == 400 else str(response.data.get('detail', 'Request denied.')),
                             'field_errors': response.data if response.status_code == 400 else {},
                             'correlation_id': str(getattr(self.request, 'correlation_id', ''))}
        return response


class StrictInput(serializers.Serializer):
    def to_internal_value(self, data):
        extra = set(data) - set(self.fields)
        if extra: raise serializers.ValidationError({name: 'Unknown field.' for name in extra})
        return super().to_internal_value(data)


class AvailabilityInput(StrictInput):
    facility_id = serializers.IntegerField(min_value=1)
    date = serializers.DateField(required=False)
    doctor_id = serializers.IntegerField(min_value=1, required=False)
    service_id = serializers.IntegerField(min_value=1, required=False)
    specialty_id = serializers.IntegerField(min_value=1, required=False)
    page = serializers.IntegerField(min_value=1, default=1)


def booking_facilities(user):
    if user.role == 'patient':
        return Facility.objects.filter(active=True, patient__user=user)
    if user.role in ['reception', 'administrator', 'doctor']:
        return Facility.objects.filter(active=True, pk__in=facility_ids(user))
    return Facility.objects.none()


class AvailabilityView(DomainView):
    def get(self, request):
        form = AvailabilityInput(data=request.query_params); form.is_valid(raise_exception=True)
        data = form.validated_data
        facility = booking_facilities(request.user).filter(pk=data['facility_id']).first()
        if not facility: raise BookingError('not_found', 'Facility not found.', 404)
        qs = Slot.objects.filter(session__facility=facility, starts_at__gt=timezone.now()).select_related(
            'session__facility', 'session__doctor', 'session__service', 'active_reservation')
        if 'date' in data: qs = qs.filter(session__date=data['date'])
        for field in ['doctor_id', 'service_id']:
            if field in data: qs = qs.filter(**{'session__' + field: data[field]})
        if 'specialty_id' in data: qs = qs.filter(session__doctor__specialty_id=data['specialty_id'])
        if request.user.role == 'doctor': qs = qs.filter(session__doctor__user=request.user)
        now = timezone.now()
        results = []
        snapshot_cache = {}
        for slot in qs:
            try: snapshot = eligible(slot, now, snapshot_cache)
            except BookingError: continue
            reservation = slot.active_reservation
            state = 'available'
            if reservation and reservation.status == 'booked': state = 'booked'
            elif reservation and reservation.status == 'held' and reservation.expires_at > now: state = 'held'
            if 'date' not in data and state != 'available': continue
            results.append({'local_date': str(slot.session.date), 'id': str(slot.pk), 'starts_at': slot.starts_at.isoformat(), 'ends_at': slot.ends_at.isoformat(),
                            'version': slot.version, 'status': state, 'doctor_id': slot.session.doctor_id,
                            'service_id': slot.session.service_id, 'doctor': slot.session.doctor.name,
                            'service': slot.session.service.name, 'fee_preview': snapshot})
        page, size = data['page'], 100
        offset = (page - 1) * size
        def page_url(number):
            query = request.query_params.copy(); query['page'] = str(number)
            return request.path + '?' + query.urlencode()
        return Response({'count': len(results), 'next': page_url(page + 1) if offset + size < len(results) else None,
                         'previous': page_url(page - 1) if page > 1 else None, 'results': results[offset:offset + size],
                         'generated_at': now.isoformat(), 'facility_timezone': facility.timezone})
