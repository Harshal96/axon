"""Eval fixture: target service (parser input only)."""

from fastapi import FastAPI

app = FastAPI()


@app.get("/api/v1/payments", operation_id="v1_payment_list")
def list_payments():
    return []
