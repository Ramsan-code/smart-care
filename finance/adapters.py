import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from django.conf import settings
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
        required = {'event_id', 'checkout_id', 'provider_reference', 'status', 'amount', 'currency'}
        if not required <= set(payload) or payload['status'] not in ['succeeded', 'failed']:
            raise ValueError('malformed')
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
    return SimulatedPaymentAdapter()
