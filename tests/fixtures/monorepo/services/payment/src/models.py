from dataclasses import dataclass


@dataclass
class PaymentRequest:
    amount: float
    currency: str


@dataclass
class ChargeResult:
    id: str
    status: str
