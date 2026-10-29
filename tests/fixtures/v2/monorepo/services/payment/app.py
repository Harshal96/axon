"""Fixture FastAPI payment service (parser input only, never run)."""

from shared.util import normalize_currency


def validate_charge(amount):
    return amount > 0


class ChargeStore:
    def save(self, amount, currency):
        return {"amount": amount, "currency": normalize_currency(currency)}


store = ChargeStore()


@app.post("/v1/charges")
def create_charge(amount, currency):
    if validate_charge(amount):
        return store.save(amount, currency)
    return {"error": "invalid"}


@app.get("/v1/charges/{charge_id}")
def get_charge(charge_id):
    return {"id": charge_id}
