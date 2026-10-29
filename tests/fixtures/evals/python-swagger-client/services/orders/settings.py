"""Eval fixture: endpoint factory indirection (parser input only)."""

import paymentscli


def get_V1Api(module, endpoint_env):
    return module.V1Api()


def GET_PAYMENTS_API():
    return get_V1Api(paymentscli, "PAYMENTS_ENDPOINT")
