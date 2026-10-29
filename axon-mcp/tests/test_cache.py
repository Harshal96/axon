import time
from axon_mcp.cache import set_cache, get_cache, invalidate_all, purge_expired

def test_cache_set_get():
    set_cache("key1", {"data": "test"})
    val = get_cache("key1")
    assert val == {"data": "test"}

def test_cache_expiration():
    set_cache("key2", {"data": "expired"}, ttl_seconds=-1)
    val = get_cache("key2")
    assert val is None

def test_cache_invalidate_all():
    set_cache("key3", {"data": "test"})
    invalidate_all()
    assert get_cache("key3") is None

def test_cache_purge_expired():
    set_cache("key4", {"data": "valid"}, ttl_seconds=100)
    set_cache("key5", {"data": "expired"}, ttl_seconds=-1)
    purge_expired()
    assert get_cache("key4") is not None
    assert get_cache("key5") is None
