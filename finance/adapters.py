import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from core.adapters import ProviderResult


def money(value):
    return Decimal(str(value)).quantize(Decimal('0.01'))


class SimulatedPaymentAdapter:
    def create_checkout(self, *, key: str, amount: Decimal, currency: str) -> ProviderResult:
        return ProviderResult(reference='sim_' + hashlib.sha256(key.encode()).hexdigest()[:20], status='pending')

    def refund(self, *, key: str, payment_reference: str, amount: Decimal) -> ProviderResult:
        return ProviderResult(reference='ref_' + hashlib.sha256(f'{key}:{payment_reference}:{amount}'.encode()).hexdigest()[:20], status='succeeded')

    def sign(self, body: bytes) -> str:
        return 'sha256=' + hmac.new(settings.SECRET_KEY.encode(), body, hashlib.sha256).hexdigest()

    def verify_callback(self, *, body: bytes, signature: str) -> dict:
        expected = self.sign(body)
        if not signature or not hmac.compare_digest(expected, signature):
            raise ValueError('invalid_signature')
        try:
            payload = json.loads(body.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError('malformed') from exc
        if not isinstance(payload, dict):
            raise ValueError('malformed')
        required = {'event_id', 'checkout_id', 'provider_reference', 'status', 'amount', 'currency'}
        if not required <= set(payload) or payload['status'] not in ['succeeded', 'failed']:
            raise ValueError('malformed')
        try:
            uuid.UUID(str(payload['checkout_id']))
            amount = Decimal(str(payload['amount']))
            if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
                raise ValueError('malformed')
            for field in ['event_id', 'provider_reference']:
                if not isinstance(payload[field], str) or not 0 < len(payload[field]) <= 64:
                    raise ValueError('malformed')
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise ValueError('malformed') from exc
        return payload

    def callback_body(self, checkout, status, event_id=None):
        payload = {
            'event_id': event_id or ('evt_' + uuid.uuid4().hex),
            'checkout_id': str(checkout.pk),
            'provider_reference': checkout.provider_reference,
            'status': status,
            'amount': str(money(checkout.amount)),
            'currency': checkout.currency,
        }
        body = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode()
        return body, self.sign(body), payload


def adapter():
    if settings.RESTORE_QUARANTINE or settings.PAYMENT_PROVIDER == 'disabled':
        raise ImproperlyConfigured('Financial submissions are disabled.')
    if settings.PAYMENT_PROVIDER == 'isolated_http':
        from operations.providers import IsolatedPaymentAdapter, require_isolation
        require_isolation()
        return IsolatedPaymentAdapter()
    if not (settings.DEBUG and settings.DEMO_MODE):
        raise ImproperlyConfigured('A production payment provider must be configured; simulation requires DEBUG and DEMO_MODE.')
    if settings.PAYMENT_PROVIDER == 'test_http':
        return DurableTestPaymentAdapter()
    if settings.PAYMENT_PROVIDER != 'simulated':
        raise ImproperlyConfigured('Unknown payment provider.')
    return SimulatedPaymentAdapter()


class DurableTestPaymentAdapter(SimulatedPaymentAdapter):
    """Local failure harness only. Server persists accepted keys in its own database."""
    def refund(self, *, key, payment_reference, amount):
        from urllib.request import Request, urlopen
        request = Request(settings.TEST_PAYMENT_URL + '/refund',
            data=json.dumps({'key': key, 'payment_reference': payment_reference, 'amount': str(amount)}).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
        return ProviderResult(reference=payload['reference'], status=payload['status'])
