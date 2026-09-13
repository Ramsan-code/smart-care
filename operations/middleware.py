import hashlib
import logging
import time
from django.conf import settings
from django.http import JsonResponse
from .telemetry import client, correlation, count, observe

LIMIT = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return n
"""


class OperationalMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = correlation.set(str(getattr(request, 'correlation_id', '')))
        start = time.monotonic()
        try:
            response = self.respond(request)
            count('requests')
            if response.status_code >= 500:
                count('server_errors')
            logging.getLogger('smartcare.request').info('request', extra={
                'event': 'request.completed', 'status': response.status_code,
                'duration_ms': round((time.monotonic() - start) * 1000, 2)})
            return response
        finally:
            observe('request', time.monotonic() - start)
            correlation.reset(token)

    def respond(self, request):
        if settings.RESTORE_QUARANTINE and request.method not in ['GET', 'HEAD', 'OPTIONS']:
            return JsonResponse({'status': 'restore_quarantine'}, status=503)
        sensitive = request.method == 'POST' and request.path.startswith(('/accounts/', '/api/v1/'))
        if sensitive:
            # The deployment only accepts HTTP from the trusted Unix-socket proxy.
            address = request.META.get('REMOTE_ADDR', '')
            if getattr(settings, 'TRUSTED_UNIX_PROXY', False) and not address:
                address = request.META.get('HTTP_X_REAL_IP', '')
            identity = str(request.user.pk) if request.user.is_authenticated else address
            digest = hashlib.sha256((settings.SECRET_KEY + identity).encode()).hexdigest()
            limit = 120 if request.user.is_authenticated else 20
            try:
                n = client().eval(LIMIT, 1, 'sc:rate:' + digest, 60)
            except Exception:
                return JsonResponse({'status': 'temporarily_unavailable'}, status=503)
            if n > limit:
                count('throttled')
                response = JsonResponse({'status': 'rate_limited'}, status=429)
                response['Retry-After'] = '60'
                return response
        return self.get_response(request)
