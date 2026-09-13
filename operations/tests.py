import io
import os
import json
import logging
from pathlib import Path
import tempfile
from unittest.mock import patch, Mock
from django.contrib.auth.models import AnonymousUser
from django.test import SimpleTestCase, TestCase, RequestFactory, override_settings
from django.http import HttpResponse
from operations.middleware import OperationalMiddleware
from operations.telemetry import SafeJSONFormatter
from operations.views import live, ready
from operations.providers import require_isolation


class SafetyTests(SimpleTestCase):
    def test_logs_exclude_messages_urls_and_raw_exception_arguments(self):
        record = logging.LogRecord('django.request', logging.ERROR, '', 1,
            '/accounts/verify/secret-token/?patient=private@example.test', (), None)
        record.operation_id = 'durable-operation'
        output = json.loads(SafeJSONFormatter().format(record))
        self.assertEqual(output['operation_id'], 'durable-operation')
        self.assertNotIn('private', json.dumps(output))
        self.assertNotIn('secret-token', json.dumps(output))

    def test_liveness_is_independent_and_readiness_fails_closed(self):
        request = RequestFactory().get('/health/live/')
        self.assertEqual(live(request).status_code, 200)
        with patch('operations.views.dependency_state', return_value={'database': False}):
            response = ready(request)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content), {'status': 'not_ready'})

    @override_settings(APP_ENV='production', DEBUG=False, DEMO_MODE=False, ISOLATED_TEST_ENABLED=True)
    def test_isolated_adapters_cannot_run_in_production(self):
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            require_isolation()

    @override_settings(RESTORE_QUARANTINE=True)
    def test_restore_blocks_worker_provider_and_http_mutations(self):
        from core.tasks import process_event, dispatch_outbox
        from finance.refunds import process_refund
        request = RequestFactory().post('/api/v1/payments/')
        with patch('finance.refunds.adapter') as provider:
            self.assertEqual(process_refund('unused'), 'quarantined')
            self.assertIsNone(process_event('unused'))
            self.assertIsNone(dispatch_outbox())
            provider.assert_not_called()
        middleware = OperationalMiddleware(lambda request: HttpResponse('unsafe'))
        self.assertEqual(middleware(request).status_code, 503)

    @override_settings(RESTORE_QUARANTINE=False, OPERATIONS_ENABLED=False)
    def test_abuse_protection_fails_closed(self):
        request = RequestFactory().post('/accounts/register/')
        request.user = AnonymousUser()
        with patch('operations.middleware.client') as store:
            store.return_value.eval.side_effect = RuntimeError()
            response = OperationalMiddleware(lambda request: HttpResponse())(request)
        self.assertEqual(response.status_code, 503)
        with patch('operations.middleware.client') as store:
            store.return_value.eval.return_value = 21
            response = OperationalMiddleware(lambda request: HttpResponse())(request)
        self.assertEqual(response.status_code, 429)

    def test_backup_authentication_precedes_restore_and_failed_dump_leaves_no_artifact(self):
        from scripts.operations.backup import backup, restore
        from cryptography.exceptions import InvalidTag
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key = root / 'key'
            key.write_text('ab' * 32)
            failed = root / 'failed.enc'
            with self.assertRaises(RuntimeError):
                backup('test_smartcare', root/'client.cnf', key, failed, '/bin/false')
            self.assertFalse(failed.exists())
            self.assertFalse(list(root.glob('*.partial')))
            script = root / 'dump'
            script.write_text('#!/bin/sh\necho "synthetic SQL"\n')
            script.chmod(0o700)
            artifact = root / 'backup.enc'
            backup('test_smartcare', root/'client.cnf', key, artifact, str(script))
            self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
            raw = bytearray(artifact.read_bytes())
            raw[-1] ^= 1
            artifact.write_bytes(raw)
            with patch('scripts.operations.backup.subprocess.run') as sql, self.assertRaises(InvalidTag):
                restore(artifact, root/'client.cnf', key, 'restore_test')
            sql.assert_not_called()
            with self.assertRaises(ValueError):
                restore(artifact, root/'client.cnf', key, 'smartcare')


@override_settings(HEALTH_TOKEN='test-health-only', DEBUG=True, SECURE_SSL_REDIRECT=False)
class MetricsTests(TestCase):
    def test_metrics_are_protected_and_labels_have_no_identity(self):
        self.assertEqual(self.client.get('/ops/metrics/').status_code, 403)
        with patch('operations.views.client') as store, patch('operations.views.dependency_state', return_value={'worker': True}):
            store.return_value.hgetall.return_value = {}
            response = self.client.get('/ops/metrics/', HTTP_AUTHORIZATION='Bearer test-health-only')
        self.assertEqual(response.status_code, 200)
        self.assertIn('smartcare_refund_unknown 0', response.content.decode())
        for prohibited in ['patient', 'appointment_id', 'operation_id', 'redis://', 'PASSWORD']:
            self.assertNotIn(prohibited, response.content.decode())


@override_settings(OPERATIONS_REDIS_URL=os.getenv('OPERATIONS_TEST_REDIS_URL', 'redis://127.0.0.1:6380/13'))
class SchedulerTests(SimpleTestCase):
    def test_only_one_scheduler_can_publish(self):
        from operations.scheduler import SingletonScheduler
        from operations.telemetry import client
        from smartcare.celery import app
        client().delete('sc:beat-lease', 'sc:beat')
        with tempfile.TemporaryDirectory() as root:
            lazy = SingletonScheduler(app=app, lazy=True)
            self.assertIsNone(client().get('sc:beat-lease'))
            lazy.close()
            first = SingletonScheduler(app=app, schedule_filename=str(Path(root)/'first'))
            try:
                with self.assertRaisesRegex(RuntimeError, 'already holds'):
                    SingletonScheduler(app=app, schedule_filename=str(Path(root)/'second'))
                with patch('celery.beat.PersistentScheduler.tick', return_value=5):
                    self.assertEqual(first.tick(), 5)
                self.assertIsNotNone(client().get('sc:beat'))
            finally:
                first.close()
        self.assertIsNone(client().get('sc:beat-lease'))
