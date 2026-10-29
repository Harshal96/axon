"""Payment service — sample Python file for testing tree-sitter parsing."""

from internal.models.models import PaymentResult
from services.payment.gateway import PaymentGateway


class PaymentService:
    """Handles payment processing for the platform."""

    def __init__(self, gateway: PaymentGateway) -> None:
        self.gateway = gateway

    def charge(self, amount: float, currency: str) -> PaymentResult:
        """Process a payment charge."""
        validated = self._validate_amount(amount)
        result = self.gateway.process(validated, currency)
        return result

    def _validate_amount(self, amount: float) -> float:
        if amount <= 0:
            raise ValueError("Amount must be positive")
        return round(amount, 2)

    def refund(self, charge_id: str) -> PaymentResult:
        """Process a refund for a previous charge."""
        return self.gateway.reverse(charge_id)


def create_payment_service() -> PaymentService:
    """Factory function for PaymentService."""
    gw = PaymentGateway()
    return PaymentService(gw)
