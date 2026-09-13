from types import SimpleNamespace

from django.test import SimpleTestCase

from .adapters import SimulatedSMSAdapter
from .services import mask_destination


class CommunicationTests(SimpleTestCase):
    def test_destinations_are_masked(self):
        self.assertEqual(mask_destination("+94771234567"), "********4567")
        self.assertEqual(mask_destination("1234"), "****")

    def test_simulated_sms_is_deterministic(self):
        adapter = SimulatedSMSAdapter()
        first = adapter.send(key="delivery-1", destination="+94771234567", text="hello")
        second = adapter.send(key="delivery-1", destination="+94771234567", text="hello")
        self.assertEqual(first, second)
        self.assertEqual(first.status, "succeeded")


# Run these against MariaDB: the production backend enforces transaction locks.
from django.test import TransactionTestCase, override_settings
from appointments.tests import BookingFixture


@override_settings(DEBUG=True, DEMO_MODE=True,
                   CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class DeliveryTransactionTests(BookingFixture, TransactionTestCase):
    def test_retry_outside_an_existing_transaction_is_atomic(self):
        from django.db import connection
        from appointments.models import Appointment
        from communications.models import Delivery
        from communications.services import retry_delivery
        from scheduling.services import BookingError

        booking = self.confirm_hold(self.make_hold())
        appointment = Appointment.objects.get(pk=booking['id'])
        delivery = Delivery.objects.create(
            key='retry-regression', facility=self.facility, appointment=appointment,
            event='confirmation', destination='+94771234567',
            masked_destination='********4567', status='failed')
        self.assertFalse(connection.in_atomic_block)
        result = retry_delivery(self.users[0], delivery.pk)
        self.assertEqual(result['status'], 'sent')
        with self.assertRaises(BookingError):
            retry_delivery(self.users[0], delivery.pk)
        delivery.refresh_from_db()
        self.assertEqual(delivery.retry_count, 1)

    def test_concurrent_retries_send_one_logical_delivery(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections, connection
        from appointments.models import Appointment
        from communications.models import Delivery
        from communications.services import retry_delivery
        from scheduling.services import BookingError
        self.assertEqual(connection.vendor, 'mysql')
        appointment = Appointment.objects.get(pk=self.confirm_hold(self.make_hold())['id'])
        delivery = Delivery.objects.create(key='race', facility=self.facility, appointment=appointment,
            event='confirmation', destination='+94771234567', masked_destination='********4567', status='failed')
        barrier = Barrier(2)
        def attempt():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return retry_delivery(self.users[0], delivery.pk)
            except BookingError as exc:
                return exc.code
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.retry_count, 1)

    def test_event_identity_survives_version_change_and_obsolete_retry_is_suppressed(self):
        from unittest.mock import patch
        from appointments.models import Appointment
        from core.models import OutboxEvent
        from core.tasks import process_event
        from core.adapters import ProviderResult
        from communications.models import Delivery
        from communications.services import retry_delivery
        self.patients[0].phone = '+94771234567'
        self.patients[0].save()
        appointment = Appointment.objects.get(pk=self.confirm_hold(self.make_hold())['id'])
        event = OutboxEvent.objects.get(topic='appointment.confirmed')
        with patch('communications.services.adapter') as provider:
            provider.return_value.send.return_value = ProviderResult('failed', 'failed')
            process_event(event.pk)
        delivery = Delivery.objects.get()
        identity, body = delivery.key, delivery.body
        appointment.version += 1
        appointment.save(update_fields=['version'])
        OutboxEvent.objects.filter(pk=event.pk).update(available_at=self.clock.return_value)
        with patch('communications.services.adapter') as provider:
            provider.return_value.send.return_value = ProviderResult('failed', 'failed')
            process_event(event.pk)
        delivery.refresh_from_db()
        self.assertEqual(Delivery.objects.count(), 1)
        self.assertEqual((delivery.key, delivery.body), (identity, body))
        appointment.state = 'cancelled'
        appointment.save(update_fields=['state'])
        with patch('communications.services.adapter') as provider:
            result = retry_delivery(self.users[0], delivery.pk)
            provider.assert_not_called()
        self.assertEqual(result['status'], 'suppressed')
