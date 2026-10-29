"""Checkout service — depends on PaymentService."""

from services.payment.payment import create_payment_service


class CheckoutService:
    """Orchestrates the checkout flow."""

    def __init__(self) -> None:
        self.payment = create_payment_service()

    def checkout(self, cart_total: float) -> dict:
        """Run the full checkout flow."""
        result = self.payment.charge(cart_total, "USD")
        return {"status": "ok", "charge": result}
