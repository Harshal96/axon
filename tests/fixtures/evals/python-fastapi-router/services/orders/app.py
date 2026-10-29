"""Eval fixture: FastAPI handler calling another service (parser input only)."""

import requests
from fastapi import FastAPI

app = FastAPI()


def reserve(item_id):
    return {"reserved": item_id}


@app.post("/orders")
def create_order(item_id):
    reservation = reserve(item_id)
    resp = requests.put(
        f"http://catalog/v1/catalog/items/{item_id}?notify=true",
        json=reservation,
    )
    return resp.json()
