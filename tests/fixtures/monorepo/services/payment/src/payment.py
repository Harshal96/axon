"""PaymentService: core payment processing."""
from models import ChargeResult, PaymentRequest


class PaymentService:
    """Handles charge and refund operations."""

    def charge(self, amount: float, currency: str) -> ChargeResult:
        """Process a payment charge."""
        request = PaymentRequest(amount=amount, currency=currency)
        return self._submit(request)

    def refund(self, charge_id: str) -> bool:
        """Refund a previous charge."""
        return self._cancel(charge_id)

    def _submit(self, request: PaymentRequest) -> ChargeResult:
        return ChargeResult(id="ch_001", status="success")

    def _cancel(self, charge_id: str) -> bool:
        return True
