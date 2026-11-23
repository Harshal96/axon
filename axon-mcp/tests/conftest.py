import pytest


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setattr("axon_mcp.cache.get_cache_db_path", lambda: tmp_path / "cache.db")
    from axon_mcp.tools import _base

    monkeypatch.setattr(_base, "_schema_checked", set())
