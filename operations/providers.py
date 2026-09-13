"""Explicit isolated adapters. These cannot be selected by production settings."""
import hashlib
import hmac
import json
from urllib.request import Request, urlopen
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from core.adapters import ProviderResult


def require_isolation():
    if (settings.APP_ENV != 'isolated' or not getattr(settings, 'ISOLATED_TEST_ENABLED', False)
            or settings.DEBUG or settings.DEMO_MODE or settings.RESTORE_QUARANTINE):
        raise ImproperlyConfigured('Isolated providers require a disposable, non-production profile.')


def submit(path, values):
    require_isolation()
    req = Request(settings.ISOLATED_PROVIDER_URL + path, data=json.dumps(values).encode(),
                  headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=5) as response:
        data = json.load(response)
    return ProviderResult(reference=data['reference'], status=data['status'])


class IsolatedPaymentAdapter:
    def create_checkout(self, *, key, amount, currency):
        return submit('/checkout', {'key': key, 'amount': str(amount), 'currency': currency})

    def refund(self, *, key, payment_reference, amount):
        return submit('/refund', {'key': key, 'payment_reference': payment_reference, 'amount': str(amount)})

    def verify_callback(self, *, body, signature):
        require_isolation()
        expected = 'sha256=' + hmac.new(settings.PAYMENT_CALLBACK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError('invalid_signature')
        # Share schema validation, but authenticate with a dedicated isolated-provider key.
        return validate_payload(body)


def validate_payload(body):
    import uuid
    from decimal import Decimal, InvalidOperation
    try:
        data = json.loads(body)
        uuid.UUID(data['checkout_id'])
        amount = Decimal(data['amount'])
        if (not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2
                or data['status'] not in ['succeeded', 'failed']):
            raise ValueError('malformed')
        for key in ['event_id', 'provider_reference']:
            if not isinstance(data[key], str) or not 0 < len(data[key]) <= 64:
                raise ValueError('malformed')
        if not isinstance(data['currency'], str) or len(data['currency']) != 3:
            raise ValueError('malformed')
        return data
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError('malformed') from exc


class IsolatedSMSAdapter:
    def send(self, *, key, destination, text):
        return submit('/sms', {'key': key, 'destination': destination, 'text': text})
