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
