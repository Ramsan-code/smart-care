from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from .adapters import SimulatedPaymentAdapter


@override_settings(SECRET_KEY='phase4-test-secret')
class PaymentAdapterTests(SimpleTestCase):
    def setUp(self):
        self.adapter = SimulatedPaymentAdapter()
        self.checkout = SimpleNamespace(
            pk='checkout-id',
            provider_reference='sim_reference',
            amount='2500.00',
            currency='LKR',
        )

    def test_signed_callback_round_trip(self):
        body, signature, payload = self.adapter.callback_body(self.checkout, 'succeeded')
        self.assertEqual(self.adapter.verify_callback(body=body, signature=signature), payload)

    def test_tampered_callback_is_rejected(self):
        body, signature, _ = self.adapter.callback_body(self.checkout, 'succeeded')
        with self.assertRaisesRegex(ValueError, 'invalid_signature'):
            self.adapter.verify_callback(body=body + b' ', signature=signature)

    def test_malformed_callback_is_rejected_after_signature_validation(self):
        body = b'not-json'
        signature = self.adapter.sign(body)
        with self.assertRaisesRegex(ValueError, 'malformed'):
            self.adapter.verify_callback(body=body, signature=signature)
