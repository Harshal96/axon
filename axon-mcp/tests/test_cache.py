from axon_mcp.cache import get_cache, invalidate_all, purge_expired, set_cache


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


def test_publication_observation_clears_legacy_and_expired_entries_per_server():
    from axon_mcp.cache import cache_epoch, get_cache_ignore_expiry, observe_publication

    set_cache("legacy", {"old": True}, namespace="a")
    set_cache("expired", {"old": True}, ttl_seconds=-1, namespace="a")
    set_cache("other", {"keep": True}, namespace="b")
    epoch = cache_epoch("a")
    observed, accepted = observe_publication("a", "publication-1", expected_epoch=epoch)
    assert accepted and observed > epoch
    assert get_cache_ignore_expiry("legacy", namespace="a") is None
    assert get_cache_ignore_expiry("expired", namespace="a") is None
    assert get_cache("other", namespace="b") == {"keep": True}
    set_cache("current", [], namespace="a", expected_epoch=observed)
    assert observe_publication("a", "", expected_epoch=observed) == (observed, True)
    assert observe_publication("a", "publication-1", expected_epoch=observed) == (observed, True)
    assert get_cache("current", namespace="a") == []


def test_old_inflight_response_cannot_restore_or_observe_old_publication():
    from axon_mcp.cache import cache_epoch, observe_publication

    old = cache_epoch("race")
    newer, _ = observe_publication("race", "new", expected_epoch=old)
    assert not set_cache("old", {"stale": True}, namespace="race", expected_epoch=old)
    assert observe_publication("race", "old", expected_epoch=old) == (newer, False)
    assert get_cache("old", namespace="race") is None
