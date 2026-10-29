"""Eval fixture: Django function views (parser input only)."""

import requests


def load_order(order_id):
    return {"id": order_id}


def list_orders(request):
    return []


def get_order(request, order_id):
    order = load_order(order_id)
    requests.get("https://shipping.example.test/tracking/{order_id}")
    return order


def report(request, year):
    return {"year": year}
