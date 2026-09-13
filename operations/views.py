import hmac
import time
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.models import Min
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from core.models import OutboxEvent
from finance.models import RefundObligation, FinanceException, Checkout
from communications.models import Delivery
from .telemetry import client, BUCKETS


def live(request):
    return JsonResponse({'status': 'alive'})


def dependency_state():
    state = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT @@read_only')
            state['database'] = not bool(cursor.fetchone()[0])
        OutboxEvent.objects.exists()  # Required schema, not merely an open socket.
    except Exception:
        state['database'] = False
    try:
        cache.set('health:probe', 'ok', 5)
        state['cache'] = cache.get('health:probe') == 'ok'
    except Exception:
        state['cache'] = False
    try:
        stamps = client().mget('sc:worker', 'sc:beat', 'sc:outbox')
        for name, stamp in zip(['worker', 'scheduler', 'outbox'], stamps):
            state[name] = stamp is not None and time.time() - float(stamp) < 45
    except Exception:
        state.update(worker=False, scheduler=False, outbox=False)
    state['integrations'] = settings.PAYMENT_PROVIDER in ['isolated_http'] and settings.APP_ENV == 'isolated'
    state['not_quarantined'] = not settings.RESTORE_QUARANTINE
    return state


def ready(request):
    ok = all(dependency_state().values())
    return JsonResponse({'status': 'ready' if ok else 'not_ready'}, status=200 if ok else 503)


def authorized(request):
    token = request.headers.get('Authorization', '')
    return bool(settings.HEALTH_TOKEN) and hmac.compare_digest(token, 'Bearer ' + settings.HEALTH_TOKEN)


def components(request):
    if not authorized(request):
        return HttpResponse(status=403)
    state = dependency_state()
    return JsonResponse(state, status=200 if all(state.values()) else 503)


def metrics(request):
    if not authorized(request):
        return HttpResponse(status=403)
    try:
        now = timezone.now()
        pending = OutboxEvent.objects.filter(processed_at__isnull=True)
        oldest = pending.aggregate(oldest=Min('created_at'))['oldest']
        values = {
            'outbox_backlog': pending.count(),
            'outbox_oldest_seconds': max(0, (now - oldest).total_seconds()) if oldest else 0,
            'outbox_dead_letters': pending.filter(dead_lettered_at__isnull=False).count(),
            'refund_unknown': RefundObligation.objects.filter(status='unknown').count(),
            'refund_failed': RefundObligation.objects.filter(status='failed').count(),
            'checkout_failed': Checkout.objects.filter(status='failed').count(),
            'notification_failed': Delivery.objects.filter(status='failed').count(),
            'notification_exhausted': Delivery.objects.filter(status='failed', next_attempt_at__isnull=True).count(),
            'reconciliation_open': FinanceException.objects.exclude(status='resolved').count(),
        }
        data = client().hgetall('sc:metrics')
        for name in ['requests', 'server_errors', 'booking_conflicts', 'database_retries', 'throttled']:
            values[name + '_total'] = float(data.get(name, 0))
        lines = [f'smartcare_{name} {value}' for name, value in values.items()]
        for name in ['request', 'worker_delay']:
            for bucket in BUCKETS:
                lines.append(f'smartcare_{name}_seconds_bucket{{le="{bucket}"}} {data.get(f"{name}_bucket_{bucket}", 0)}')
            lines.extend([
                f'smartcare_{name}_seconds_bucket{{le="+Inf"}} {data.get(name + "_count", 0)}',
                f'smartcare_{name}_seconds_count {data.get(name + "_count", 0)}',
                f'smartcare_{name}_seconds_sum {data.get(name + "_sum", 0)}'])
        lines.extend(f'smartcare_component_up{{component="{name}"}} {int(ok)}'
                     for name, ok in dependency_state().items())
        return HttpResponse('\n'.join(lines) + '\n', content_type='text/plain; version=0.0.4')
    except Exception:
        return HttpResponse('smartcare_scrape_success 0\n', status=503, content_type='text/plain')
