from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from .adapters import SimulatedPaymentAdapter


@override_settings(SECRET_KEY='phase4-test-secret')
class PaymentAdapterTests(SimpleTestCase):
    def setUp(self):
        self.adapter = SimulatedPaymentAdapter()
        self.checkout = SimpleNamespace(
            pk='00000000-0000-0000-0000-000000000001',
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


class PaymentSafetyTests(SimpleTestCase):
    def test_simulation_adapter_requires_both_demo_flags(self):
        from django.core.exceptions import ImproperlyConfigured
        from .adapters import adapter
        for debug, demo in [(False, False), (False, True), (True, False)]:
            with self.subTest(debug=debug, demo=demo):
                with override_settings(DEBUG=debug, DEMO_MODE=demo):
                    with self.assertRaises(ImproperlyConfigured):
                        adapter()
        with override_settings(DEBUG=True, DEMO_MODE=True):
            self.assertIsInstance(adapter(), SimulatedPaymentAdapter)

    @override_settings(DEBUG=False, DEMO_MODE=True)
    def test_simulation_service_rejects_before_database_access(self):
        from scheduling.services import BookingError
        from .services import simulate_checkout
        with self.assertRaises(BookingError) as caught:
            simulate_checkout(SimpleNamespace(is_authenticated=True), 'unused', 'success')
        self.assertEqual(caught.exception.code, 'not_found')

    def test_production_urlconf_has_no_simulation_route(self):
        import importlib
        import smartcare.urls
        try:
            with override_settings(DEBUG=False, DEMO_MODE=True):
                urls = importlib.reload(smartcare.urls)
                self.assertFalse(any('simulate/' in str(p.pattern) for p in urls.urlpatterns))
        finally:
            importlib.reload(smartcare.urls)

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_real_multipart_upload_reaches_import_service(self):
        from unittest.mock import patch
        from django.core.files.uploadedfile import SimpleUploadedFile
        from rest_framework.test import APIRequestFactory, force_authenticate
        from .api import GatewayImportView
        upload = SimpleUploadedFile('gateway.csv',
            b'transaction_reference,amount,status,provider_event_id\n',
            content_type='text/csv')
        request = APIRequestFactory().post('/api/v1/finance/import/',
            {'facility_id': '1', 'file': upload}, format='multipart')
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
        with patch('finance.api.import_gateway_csv', return_value={'status': 'processed'}) as importer:
            response = GatewayImportView.as_view()(request)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(importer.call_args.args[1], 1)
        self.assertEqual(importer.call_args.args[2].name, 'gateway.csv')
