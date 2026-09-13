import hashlib
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from core.adapters import ProviderResult


class SimulatedSMSAdapter:
    def send(self, *, key, destination, text):
        reference = "sms_" + hashlib.sha256(f"{key}:{destination}:{text}".encode()).hexdigest()[:20]
        return ProviderResult(reference=reference, status="succeeded")


def adapter():
    if settings.RESTORE_QUARANTINE:
        raise ImproperlyConfigured('Notifications are disabled during restore quarantine.')
    if getattr(settings, 'SMS_PROVIDER', '') == 'isolated_http':
        from operations.providers import IsolatedSMSAdapter, require_isolation
        require_isolation()
        return IsolatedSMSAdapter()
    if not (settings.DEBUG and settings.DEMO_MODE):
        raise ImproperlyConfigured('A production SMS adapter is required outside demo mode.')
    return SimulatedSMSAdapter()
