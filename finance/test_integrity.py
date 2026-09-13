from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch
import csv
import io
import uuid

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction, IntegrityError
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient
from accounts.models import User, Membership
from appointments.tests import BookingFixture
from appointments.models import Appointment
from appointments.services import command
from core.adapters import ProviderResult
from core.models import OutboxEvent
from core.tasks import process_event
from scheduling.services import BookingError
from .models import Payment, DoctorPayable, SettlementBatch, SettlementLine, RefundObligation, Checkout
from .services import record_counter_payment, record_refund, appointment_financials, apply_signed_callback, open_checkout
from .phase6 import create_settlement, change_settlement, add_refund_adjustment, export_settlement
from .refunds import process_refund


@override_settings(DEBUG=True, DEMO_MODE=True,
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class FinanceIntegrityTests(BookingFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.finance = User.objects.create_user('finance@example.test', role='finance')
        Membership.objects.create(user=self.finance, facility=self.facility, finance_approver=True)
        self.appointment = Appointment.objects.get(pk=self.confirm_hold(self.make_hold())['id'])
        with transaction.atomic():
            record_counter_payment(self.reception, self.appointment.pk, self.appointment.version, '2500.00')
        self.appointment.refresh_from_db()
        self.charge = Payment.objects.get(kind='charge')
        self.payable = DoctorPayable.objects.create(facility=self.facility, doctor=self.doctor,
            appointment=self.appointment, source_payment=self.charge, amount='2000.00')

    def race(self, functions):
        self.assertEqual(connection.vendor, 'mysql', 'Concurrency tests require MariaDB/InnoDB.')
        barrier = Barrier(len(functions))
        def run(fn):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return fn()
            except BookingError as exc:
                return exc.code
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            return list(pool.map(run, functions))

    def paid_batch(self):
        batch = create_settlement(self.finance, self.facility.pk, 'original')
        change_settlement(self.finance, batch['id'], 'review')
        change_settlement(self.admin, batch['id'], 'approve')
        change_settlement(self.finance, batch['id'], 'paid')
        return batch['id']

    def request_refund(self, amount='500.00', key=None):
        appointment = Appointment.objects.get(pk=self.appointment.pk)
        key = key or str(uuid.uuid4())
        return command(self.reception, 'payment.refund', key, {'amount': amount},
            lambda: record_refund(self.reception, appointment.pk, appointment.version, amount))

    def settled_refund(self, amount='500.00'):
        operation = self.request_refund(amount)
        process_refund(operation['obligation_id'])
        return Payment.objects.get(obligation_id=operation['obligation_id'])

    def test_concurrent_batches_allocate_payable_once(self):
        result = self.race([
            lambda: create_settlement(self.finance, self.facility.pk, 'batch-a'),
            lambda: create_settlement(self.finance, self.facility.pk, 'batch-b')])
        self.assertTrue(all(isinstance(r, dict) for r in result), result)
        self.assertEqual(SettlementLine.objects.filter(original_payable=self.payable).count(), 1)
        self.assertEqual(sum(r['lines'] for r in result), 1)

    def test_database_rejects_duplicate_original_allocation(self):
        self.paid_batch()
        batch = SettlementBatch.objects.create(facility=self.facility, reference='duplicate', created_by=self.finance)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SettlementLine.objects.create(batch=batch, payable=self.payable,
                original_payable=self.payable, doctor=self.doctor, amount=2000)

    def test_settlement_replay_and_conflict(self):
        first = create_settlement(self.finance, self.facility.pk, 'same')
        self.assertEqual(first, create_settlement(self.finance, self.facility.pk, 'same'))
        with self.assertRaises(BookingError):
            create_settlement(self.finance, self.facility.pk, 'same', currency='USD')
        self.assertEqual(SettlementBatch.objects.count(), 1)

    def test_settlement_api_idempotency_and_export(self):
        client = APIClient()
        client.force_authenticate(self.finance)
        body = {'facility_id': self.facility.pk, 'reference': 'api'}
        first = client.post('/api/v1/finance/settlements/', body, format='json', HTTP_IDEMPOTENCY_KEY='batch')
        self.assertEqual(first.status_code, 201, first.data)
        second = client.post('/api/v1/finance/settlements/', body, format='json', HTTP_IDEMPOTENCY_KEY='batch')
        self.assertEqual(first.data, second.data)
        export = client.get(f"/api/v1/finance/settlements/{first.data['id']}/export/")
        self.assertEqual(export.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(export.content.decode())))
        self.assertEqual(sum(Decimal(r['amount']) + Decimal(r['adjustment']) for r in rows),
                         Decimal(first.data['total']))

    def test_self_approval_and_cross_facility_denied(self):
        batch = create_settlement(self.finance, self.facility.pk, 'self')
        change_settlement(self.finance, batch['id'], 'review')
        with self.assertRaises(BookingError):
            change_settlement(self.finance, batch['id'], 'approve')
        client = APIClient()
        client.force_authenticate(self.finance)
        response = client.post('/api/v1/finance/settlements/',
            {'facility_id': self.other.pk, 'reference': 'forbidden'}, format='json', HTTP_IDEMPOTENCY_KEY='other')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(SettlementBatch.objects.count(), 1)

    def test_adjustment_requires_paid_batch_membership_and_refund(self):
        paid = self.paid_batch()
        refund = self.settled_refund()
        wrong = SettlementBatch.objects.create(facility=self.facility, reference='wrong',
            created_by=self.finance, status='paid')
        for batch_id, refund_id in [(wrong.pk, refund.pk), (paid, self.charge.pk), (paid, uuid.uuid4())]:
            with self.subTest(batch=batch_id, refund=refund_id), self.assertRaises(BookingError):
                add_refund_adjustment(self.finance, batch_id, self.payable.pk, '400', refund_id)
        self.assertEqual(SettlementLine.objects.count(), 1)

    def test_refund_share_replay_and_immutable_paid_records(self):
        paid = self.paid_batch()
        refund = self.settled_refund()
        with self.assertRaises(BookingError):
            add_refund_adjustment(self.finance, paid, self.payable.pk, '500', refund.pk)
        result = add_refund_adjustment(self.finance, paid, self.payable.pk, '400', refund.pk)
        self.assertEqual(result, add_refund_adjustment(self.finance, paid, self.payable.pk, '400', refund.pk))
        self.assertEqual(result['total'], '-400.00')
        self.assertEqual(SettlementLine.objects.get(refund=refund).original_line.batch_id, paid)
        batch = SettlementBatch.objects.get(pk=paid)
        batch.adjustment = 1
        with self.assertRaises(ValidationError):
            batch.save()
        self.assertEqual(SettlementBatch.objects.get(pk=paid).adjustment, 0)

    def test_concurrent_refund_adjustment_replay_allocates_once(self):
        paid = self.paid_batch()
        refund = self.settled_refund()
        result = self.race([lambda: add_refund_adjustment(self.finance, paid, self.payable.pk, '400', refund.pk)] * 2)
        self.assertEqual(result[0]['id'], result[1]['id'])
        self.assertEqual(SettlementLine.objects.filter(refund=refund).count(), 1)

    def test_concurrent_refunds_reserve_balance(self):
        result = self.race([lambda: self.request_refund('2000.00')] * 2)
        self.assertEqual(sum(isinstance(r, dict) for r in result), 1, result)
        self.assertEqual(RefundObligation.objects.count(), 1)
        self.assertEqual(appointment_financials(self.appointment)['refundable'], '500.00')
        self.assertEqual(Payment.objects.filter(kind='refund').count(), 0)

    def test_refund_replay_and_rollback_has_no_provider_effect(self):
        with patch('finance.refunds.adapter') as provider:
            try:
                with transaction.atomic():
                    self.request_refund(key='rollback')
                    raise RuntimeError('rollback')
            except RuntimeError:
                pass
            provider.assert_not_called()
        self.assertFalse(RefundObligation.objects.exists())
        first = self.request_refund(key='retry')
        second = self.request_refund(key='retry')
        self.assertEqual(first, second)
        self.assertEqual(RefundObligation.objects.count(), 1)

    def test_provider_outcomes_and_duplicate_processing(self):
        for outcome in ['succeeded', 'failed', 'pending', 'unknown', 'timeout']:
            with self.subTest(outcome=outcome):
                operation = self.request_refund('100.00')
                with patch('finance.refunds.adapter') as provider:
                    if outcome == 'timeout':
                        provider.return_value.refund.side_effect = TimeoutError()
                    else:
                        provider.return_value.refund.return_value = ProviderResult('provider-result', outcome)
                    status = process_refund(operation['obligation_id'])
                    provider.return_value.refund.assert_called_once()
                row = RefundObligation.objects.get(pk=operation['obligation_id'])
                expected = {'succeeded': 'refunded', 'timeout': 'unknown'}.get(outcome, outcome)
                self.assertEqual(status, expected)
                self.assertEqual(row.payments.count(), int(outcome == 'succeeded'))
                if outcome == 'succeeded':
                    process_refund(row.pk)
                    self.assertEqual(row.payments.count(), 1)

    def test_provider_call_runs_outside_database_transaction(self):
        operation = self.request_refund()
        def refund(**kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(kwargs['key'], f"refund:{operation['obligation_id']}")
            return ProviderResult('independent', 'succeeded')
        with patch('finance.refunds.adapter') as provider:
            provider.return_value.refund.side_effect = refund
            process_refund(operation['obligation_id'])

    def test_duplicate_and_out_of_order_callbacks(self):
        # Use a fresh unpaid appointment; restore the fixture slot for another booking.
        slot = self.session.slots.exclude(pk=self.slot.pk).first()
        from appointments.services import hold, confirm
        h = hold(self.users[1], slot.pk, slot.version)
        booked = confirm(self.users[1], h['id'], 1, 'demo-v1', 'new_visit', 'counter_due')
        appointment = Appointment.objects.get(pk=booked['id'])
        with transaction.atomic():
            data = open_checkout(self.users[1], appointment, '2500', 'appointment')
        checkout = Checkout.objects.get(pk=data['id'])
        from .adapters import adapter
        for index, status in enumerate(['succeeded', 'succeeded', 'failed']):
            body, signature, _ = adapter().callback_body(checkout, status, event_id=f'callback-{index}')
            apply_signed_callback(body, signature)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, 'succeeded')
        self.assertEqual(Payment.objects.filter(checkout=checkout).count(), 1)

    def test_outbox_poison_does_not_block_valid_work(self):
        poison = OutboxEvent.objects.create(key='poison', topic='unsupported', payload={})
        good = OutboxEvent.objects.create(key='good', topic='demo.ping', payload={})
        with self.assertRaises(ValueError):
            process_event(poison.pk)
        process_event(good.pk)
        poison.refresh_from_db()
        good.refresh_from_db()
        self.assertIsNotNone(good.processed_at)
        self.assertGreater(poison.available_at, self.clock.return_value)
        self.assertEqual(poison.attempts, 1)

    def test_concurrent_adjustments_cannot_exceed_payable_even_with_bad_legacy_refunds(self):
        paid = self.paid_batch()
        refunds = [Payment.objects.create(facility=self.facility, appointment=self.appointment,
            kind='refund', method='hosted', amount='1500.00', currency='LKR',
            provider_event_id=f'legacy-{i}', provider_reference=f'legacy-{i}') for i in range(2)]
        result = self.race([
            lambda: add_refund_adjustment(self.finance, paid, self.payable.pk, '1200', refunds[0].pk),
            lambda: add_refund_adjustment(self.finance, paid, self.payable.pk, '1200', refunds[1].pk)])
        self.assertEqual(sum(isinstance(r, dict) for r in result), 1)
        self.assertEqual(SettlementLine.objects.filter(kind='refund').count(), 1)

    def test_booking_confirmation_races_session_cancellation(self):
        from appointments.operations import cancel_session
        from appointments.services import hold, confirm
        slot = self.session.slots.exclude(pk=self.slot.pk).first()
        h = hold(self.users[1], slot.pk, slot.version)
        result = self.race([
            lambda: confirm(self.users[1], h['id'], 1, 'demo-v1', 'new_visit', 'counter_due'),
            lambda: cancel_session(self.reception, self.session.pk, self.session.version, 'clinic closed')])
        self.session.refresh_from_db()
        self.assertFalse(self.session.active)
        self.assertFalse(Appointment.objects.filter(reservation__slot__session=self.session, state='confirmed').exists())
        self.assertFalse(self.session.slots.filter(active_reservation__isnull=False).exists())

    def test_resolution_version_is_checked_under_lock(self):
        from appointments.operations import cancel_session, resolve_cancellation
        result = cancel_session(self.reception, self.session.pk, self.session.version, 'clinic closed')
        resolution = result['resolution_ids'][0]
        results = self.race([lambda: resolve_cancellation(self.reception, resolution, 1, 'contacted')] * 2)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertIn('stale_version', results)

    def test_adjustment_api_rejects_missing_refund_and_foreign_finance(self):
        paid = self.paid_batch()
        client = APIClient()
        client.force_authenticate(self.finance)
        response = client.post('/api/v1/finance/settlements/refund-adjustments/',
            {'paid_batch_id': paid, 'payable_id': self.payable.pk, 'amount': '10'}, format='json',
            HTTP_IDEMPOTENCY_KEY='adjustment')
        self.assertEqual(response.status_code, 400)
        stranger = User.objects.create_user('stranger@example.test', role='finance')
        Membership.objects.create(user=stranger, facility=self.other, finance_approver=True)
        client.force_authenticate(stranger)
        for method, url in [('get', f'/api/v1/finance/settlements/{paid}/export/'),
                            ('post', f'/api/v1/finance/settlements/{paid}/approve/'),
                            ('post', f'/api/v1/finance/settlements/{paid}/paid/')]:
            self.assertEqual(getattr(client, method)(url, HTTP_IDEMPOTENCY_KEY='forbidden-' + url).status_code, 403)

    def test_migration_preflight_preserves_conflicting_legacy_allocations(self):
        from django.db.migrations.executor import MigrationExecutor
        original = create_settlement(self.finance, self.facility.pk, 'legacy-original')
        latest = ('finance', '0005_settlementmutex_payment_refund_operation_one_ledger_and_more')
        previous = ('finance', '0003_doctorpayable_gatewayimport_gatewayimportrow_and_more')
        executor = MigrationExecutor(connection)
        executor.migrate([previous])
        old_apps = executor.loader.project_state([previous]).apps
        Line = old_apps.get_model('finance', 'SettlementLine')
        Batch = old_apps.get_model('finance', 'SettlementBatch')
        duplicate_batch = Batch.objects.create(facility_id=self.facility.pk, reference='legacy-duplicate',
                                               created_by_id=self.finance.pk)
        duplicate = Line.objects.create(batch_id=duplicate_batch.pk, payable_id=self.payable.pk,
                                        doctor_id=self.doctor.pk, amount=2000)
        try:
            with self.assertRaisesRegex(RuntimeError, 'Financial migration requires reconciliation'):
                MigrationExecutor(connection).migrate([latest])
            self.assertEqual(Line.objects.filter(payable_id=self.payable.pk).count(), 2)
            with connection.cursor() as cursor:
                names = [c.name for c in connection.introspection.get_table_description(cursor, 'finance_settlementline')]
            self.assertNotIn('original_payable_id', names)  # Preflight ran before nontransactional DDL.
        finally:
            # Remove only this test's synthetic conflicting row to restore the test schema.
            duplicate.delete()
            MigrationExecutor(connection).migrate([latest])
        self.assertEqual(SettlementLine.objects.get(batch_id=original['id']).original_payable_id, self.payable.pk)

    def test_production_configuration_fails_clearly(self):
        from django.core.checks import run_checks
        with override_settings(DEBUG=False, DEMO_MODE=True):
            self.assertIn('finance.E001', [error.id for error in run_checks()])

    def test_transferred_credit_can_be_refunded_once_from_original_collection(self):
        from appointments.services import hold
        from .services import request_reschedule, cancel_appointment
        slot = self.session.slots.exclude(pk=self.slot.pk).first()
        h = hold(self.users[0], slot.pk, slot.version)
        with transaction.atomic():
            moved = request_reschedule(self.users[0], self.appointment.pk, h['id'], self.appointment.version, h['version'])
        replacement = Appointment.objects.get(pk=moved['replacement']['id'])
        self.assertEqual(appointment_financials(self.appointment)['refundable'], '0.00')
        self.assertEqual(appointment_financials(replacement)['refundable'], '2500.00')
        with transaction.atomic():
            cancelled = cancel_appointment(self.reception, replacement.pk, replacement.version)
        operation = RefundObligation.objects.get(pk=cancelled['obligation_id'])
        self.assertEqual(operation.source_payment_id, self.charge.pk)
        process_refund(operation.pk)
        self.assertEqual(Payment.objects.get(obligation=operation).appointment_id, replacement.pk)
        self.assertEqual(appointment_financials(replacement)['refundable'], '0.00')

    def test_refund_is_split_into_durable_operations_for_multiple_collections(self):
        from appointments.services import hold, confirm
        slot = self.session.slots.exclude(pk=self.slot.pk).first()
        h = hold(self.users[1], slot.pk, slot.version)
        booking = confirm(self.users[1], h['id'], h['version'], 'demo-v1', 'new_visit', 'counter_due')
        appointment = Appointment.objects.get(pk=booking['id'])
        for amount in ['1000', '1500']:
            with transaction.atomic():
                record_counter_payment(self.reception, appointment.pk, appointment.version, amount)
            appointment.refresh_from_db()
        operation = record_refund(self.reception, appointment.pk, appointment.version, '2500')
        self.assertEqual(len(operation['obligation_ids']), 2)
        for operation_id in operation['obligation_ids']:
            process_refund(operation_id)
        self.assertEqual(appointment_financials(appointment)['refundable'], '0.00')
        self.assertEqual(Payment.objects.filter(appointment=appointment, kind='refund').count(), 2)

    def test_settlement_transition_api_replay(self):
        batch = create_settlement(self.finance, self.facility.pk, 'transition-replay')
        client = APIClient()
        client.force_authenticate(self.finance)
        url = f"/api/v1/finance/settlements/{batch['id']}/review/"
        first = client.post(url, {}, format='json', HTTP_IDEMPOTENCY_KEY='review')
        replay = client.post(url, {}, format='json', HTTP_IDEMPOTENCY_KEY='review')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data, replay.data)

    def test_refund_operator_retry_preserves_identity_and_reservation(self):
        from django.core.management import call_command
        from io import StringIO
        operation_data = self.request_refund()
        operation = RefundObligation.objects.get(pk=operation_data['obligation_id'])
        operation.status = 'unknown'
        operation.attempts = 8
        operation.next_attempt_at = None
        operation.failure_owner = 'finance'
        operation.save()
        call_command('retry_refund', str(operation.pk), stdout=StringIO())
        operation.refresh_from_db()
        self.assertEqual(operation.attempts, 8)
        self.assertEqual(appointment_financials(self.appointment)['refundable'], '2000.00')
        with patch('finance.refunds.adapter') as provider:
            provider.return_value.refund.return_value = ProviderResult('resolved', 'succeeded')
            process_refund(operation.pk)
            self.assertEqual(provider.return_value.refund.call_args.kwargs['key'], f'refund:{operation.pk}')
        self.assertEqual(Payment.objects.filter(obligation=operation).count(), 1)

    def test_unknown_outbox_event_is_quarantined_after_retry_budget(self):
        poison = OutboxEvent.objects.create(key='bounded-poison', topic='unknown', payload={})
        for _ in range(8):
            OutboxEvent.objects.filter(pk=poison.pk).update(available_at=self.clock.return_value)
            with self.assertRaises(ValueError):
                process_event(poison.pk)
        poison.refresh_from_db()
        self.assertEqual(poison.attempts, 8)
        self.assertIsNotNone(poison.dead_lettered_at)
        self.assertIsNone(poison.processed_at)
