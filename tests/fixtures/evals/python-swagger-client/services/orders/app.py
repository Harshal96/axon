"""Eval fixture: handler calling through a generated Swagger client.

Mirrors the generated-client pattern: settings factory -> retry wrappers -> fn.
Parser input only; never executed.
"""

import settings
from fastapi import FastAPI
from retry_utils import call_payments_api

app = FastAPI()


@app.get("/orders")
def orders_view():
    payments_api = settings.GET_PAYMENTS_API()
    return call_payments_api(payments_api.v1_payment_list, role="admin")
