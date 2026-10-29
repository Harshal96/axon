"""CheckoutFlow: orchestrates payment for checkout."""
from payment_client import PaymentClient


class CheckoutFlow:
    def __init__(self):
        self.client = PaymentClient()

    def process_payment(self, amount: float, currency: str) -> dict:
        result = self.client.charge(amount, currency)
        return {"charge_id": result.id, "status": result.status}
