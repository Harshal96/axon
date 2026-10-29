"""Eval fixture: higher-order retry wrappers (parser input only)."""


def _retry_loop(fn):
    try:
        return fn()
    except Exception:
        return fn()


def call_with_retry(fn, *args, **kwargs):
    return _retry_loop(lambda: fn(*args, **kwargs))


def call_payments_api(fn, *args, **kwargs):
    return call_with_retry(fn, *args, **kwargs)
