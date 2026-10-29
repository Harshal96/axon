class PaymentClient:
    def charge(self, amount: float, currency: str):
        return type("ChargeResult", (), {"id": "ch_001", "status": "success"})()
