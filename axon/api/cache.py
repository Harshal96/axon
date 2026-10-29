import json

OP_TTL = {
    "graph_diff":                  3600,
    "symbol_history":               300,
    "api_history":                  300,
    "time_travel_blast_radius":    3600,
    "breaking_alerts":               30,
}

_MOCK_CACHE = {}


def _cache_key(op_name: str, params: dict) -> str:
    return f"{op_name}:{json.dumps(params, sort_keys=True, default=str)}"

async def cache_get(op_name: str, params: dict) -> str | None:
    key = _cache_key(op_name, params)
    return _MOCK_CACHE.get(key)

async def cache_set(op_name: str, params: dict, value: str) -> None:
    key = _cache_key(op_name, params)
    _MOCK_CACHE[key] = value

async def cache_invalidate() -> None:
    _MOCK_CACHE.clear()

async def cache_invalidate_pattern(pattern: str) -> None:
    keys_to_delete = [k for k in _MOCK_CACHE.keys() if pattern in k]
    for k in keys_to_delete:
        del _MOCK_CACHE[k]

def cached(ttl: int = 300):
    def decorator(func):
        import functools
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            return await func(*args, **kwargs)
        return wrapper
    return decorator
