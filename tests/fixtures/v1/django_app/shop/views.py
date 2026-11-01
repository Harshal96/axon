"""Fixture Django views (parser input only)."""


def load_order(order_id):
    return {"id": order_id}


def list_orders(request):
    return []


def get_order(request, order_id):
    return load_order(order_id)
