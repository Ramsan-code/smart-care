import hashlib

from core.adapters import ProviderResult


class SimulatedSMSAdapter:
    def send(self, *, key, destination, text):
        reference = "sms_" + hashlib.sha256(f"{key}:{destination}:{text}".encode()).hexdigest()[:20]
        return ProviderResult(reference=reference, status="succeeded")


def adapter():
    return SimulatedSMSAdapter()
