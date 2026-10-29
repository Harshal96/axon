"""Fixture FastAPI checkout service (parser input only)."""

import httpx
from payment_client import PaymentClient

payment_client = PaymentClient()


def price_cart(items):
    return sum(items)


@app.post("/checkout")
def place_order(items, currency):
    total = price_cart(items)
    charge = payment_client.create_charge(total, currency)
    return {"charge": charge}


@app.get("/checkout/{order_id}/status")
def order_status(order_id):
    resp = httpx.get("http://payment/v1/charges/{order_id}")
    return resp.json()
