from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from configuration.models import Doctor, ScheduleRule, Leave, FeeVersion, PolicyVersion
from core.services import audit
from .models import Session, Slot


class BookingError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def effective(qs, day):
    return qs.filter(effective_from__lte=day).filter(Q(effective_until__isnull=True) | Q(effective_until__gt=day))


def prices(session):
    fee = effective(FeeVersion.objects.filter(service=session.service, facility=session.facility), session.date).first()
    policy = effective(PolicyVersion.objects.filter(facility=session.facility), session.date).first()
    if not fee or not policy:
        raise BookingError('configuration_missing', 'No fee or booking policy is published for this date.')
    return {'fee_version_id': fee.pk, 'policy_version_id': policy.pk, 'policy_version': policy.version,
            'doctor_fee': str(fee.doctor_fee), 'facility_fee': str(fee.facility_fee), 'tax': str(fee.tax),
            'discount': str(fee.discount), 'total': str(fee.total), 'currency': session.facility.currency,
            'hold_minutes': policy.hold_minutes, 'release_days': policy.release_days,
            'cancellation_hours': policy.cancellation_hours, 'timezone': session.facility.timezone,
            'doctor': session.doctor.name, 'service': session.service.name, 'facility': session.facility.name,
            'duration_minutes': session.snapshot.get('duration_minutes', session.service.duration_minutes)}


def eligible(slot, now=None, cache=None):
    now = now or timezone.now()
    s = slot.session
    if not s.active or not s.facility.active or not s.doctor.active or not s.doctor.verified or not s.service.active or slot.blocked or slot.starts_at <= now:
        raise BookingError('unavailable', 'This appointment time is no longer available.')
    cache = cache if cache is not None else {}
    if s.pk not in cache:
        on_leave = Leave.objects.filter(doctor=s.doctor, starts_on__lte=s.date, ends_on__gte=s.date).exists()
        cache[s.pk] = (on_leave, prices(s))
    on_leave, snapshot = cache[s.pk]
    if on_leave:
        raise BookingError('unavailable', 'The doctor is on leave on this date.')
    local_day = now.astimezone(ZoneInfo(s.facility.timezone)).date()
    if s.date < local_day or s.date >= local_day + timedelta(days=snapshot['release_days']):
        raise BookingError('outside_release_window', 'This date is outside the booking release window.')
    return snapshot


@transaction.atomic
def generate(rule_id, day, actor=None):
    # Serialize all generators and booking writers for the doctor, including cross-service sessions.
    hint = ScheduleRule.objects.get(pk=rule_id)
    Doctor.objects.select_for_update().get(pk=hint.doctor_id)
    rule = ScheduleRule.objects.select_related('doctor', 'service', 'facility').get(pk=rule_id)
    existing = Session.objects.filter(rule=rule, date=day).first()
    if existing:
        return existing, False  # Published inventory is never rewritten by regeneration.
    if not effective(ScheduleRule.objects.filter(pk=rule.pk), day).exists() or rule.weekday != day.weekday():
        return None, False
    if not rule.facility.active or not rule.doctor.active or not rule.doctor.verified or not rule.service.active:
        return None, False
    if Leave.objects.filter(doctor=rule.doctor, starts_on__lte=day, ends_on__gte=day).exists():
        return None, False
    rule.full_clean()
    tz = ZoneInfo(rule.facility.timezone)
    start, end = [datetime.combine(day, t, tzinfo=tz) for t in (rule.starts_at, rule.ends_at)]
    if Session.objects.filter(doctor=rule.doctor, starts_at__lt=end, ends_at__gt=start).exists():
        raise BookingError('session_overlap', 'This doctor already has an overlapping published session.')
    session = Session.objects.create(rule=rule, facility=rule.facility, doctor=rule.doctor, service=rule.service,
                                     date=day, starts_at=start, ends_at=end,
                                     snapshot={'duration_minutes': rule.service.duration_minutes, 'buffer_minutes': rule.buffer_minutes,
                                               'break_start': str(rule.break_start) if rule.break_start else None,
                                               'break_end': str(rule.break_end) if rule.break_end else None})
    duration = timedelta(minutes=rule.service.duration_minutes)
    step = duration + timedelta(minutes=rule.buffer_minutes)
    break_start = datetime.combine(day, rule.break_start, tzinfo=tz) if rule.break_start else None
    break_end = datetime.combine(day, rule.break_end, tzinfo=tz) if rule.break_end else None
    slots = []
    cursor = start
    while cursor + duration <= end:
        if break_start and cursor < break_end and cursor + step > break_start:
            cursor = break_end
            continue
        slots.append(Slot(session=session, starts_at=cursor, ends_at=cursor + duration))
        cursor += step
    Slot.objects.bulk_create(slots)
    audit(actor, 'session.published', session.pk, rule.facility, {'slot_count': len(slots), 'date': str(day)})
    return session, True
