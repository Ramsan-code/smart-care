"""Contracts only. Hosted payments use the simulated adapter; SMS belongs to a later phase."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

@dataclass(frozen=True)
class ProviderResult:
    reference: str
    status: str

class PaymentAdapter(Protocol):
    def create_checkout(self, *, key: str, amount: Decimal, currency: str) -> ProviderResult: ...
    def refund(self, *, key: str, payment_reference: str, amount: Decimal) -> ProviderResult: ...
    def verify_callback(self, *, body: bytes, signature: str) -> dict: ...

class SMSAdapter(Protocol):
    def send(self, *, key: str, destination: str, text: str) -> ProviderResult: ...
