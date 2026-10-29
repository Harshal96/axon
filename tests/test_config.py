from __future__ import annotations

from axon.config import Settings, get_settings


def test_default_settings() -> None:
    """Settings should load with defaults when no .env overrides are applied."""
    s = Settings(
        _env_file=None,  # ignore any .env file
    )
    assert s.neo4j_uri == "bolt://localhost:7687"
    assert s.neo4j_username == "neo4j"
    assert s.neo4j_password == "axon_dev"
    assert s.neo4j_database == "neo4j"
    assert s.api_port == 8080
    assert s.log_level == "INFO"
    assert s.graph_staleness_threshold_seconds == 300


def test_get_settings_returns_same_instance() -> None:
    """get_settings() is cached, so it should always return the same object."""
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2


def test_settings_graph_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.graph_default_blast_radius_depth == 5
    assert s.graph_max_blast_radius_depth == 10
    assert s.graph_default_subgraph_depth == 2
    assert s.graph_default_callers_depth == 1
    assert s.graph_default_callees_depth == 3
    assert s.graph_search_limit_default == 20
    assert s.graph_search_limit_max == 100


def test_settings_connection_pool_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.neo4j_max_connection_pool_size == 50
    assert s.neo4j_connection_timeout_seconds == 30
    assert s.neo4j_max_transaction_retry_time_seconds == 30
