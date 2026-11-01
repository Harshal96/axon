from fastapi.testclient import TestClient

from axon.api.app import create_app


def test_only_current_v1_api_is_mounted():
    app = create_app()
    client = TestClient(app)
    assert client.get("/api/v1/services").status_code == 200
    health = client.get("/api/v1/health").json()
    assert health["status"] == "ok"
    assert health["neo4j"] == "disabled"
    assert health["graph_stats"] == {"nodes": 0, "edges": 0}
    assert client.get("/api/v2/services").status_code == 404
    for path in ["/api/v1/ops/search", "/api/v1/graph/search", "/api/v1/commits"]:
        assert client.get(path).status_code == 404
    paths = app.openapi()["paths"]
    assert all(path == "/health" or path.startswith("/api/v1/") for path in paths)


def test_health_reports_unreachable_persistent_store(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    app = create_app()
    monkeypatch.setattr(
        "axon.api.routes.health.get_settings", lambda: SimpleNamespace(axon_store="neo4j")
    )
    monkeypatch.setattr("axon.api.routes.health.verify_connectivity", AsyncMock(return_value=False))
    health = TestClient(app).get("/api/v1/health").json()
    assert health["status"] == "unhealthy"
    assert health["neo4j"] == "unreachable"
    assert health["graph_stats"] is None


def test_health_reports_graph_load_failure(monkeypatch):
    app = create_app()

    def unavailable():
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(app.state.v1_store, "load", unavailable)
    health = TestClient(app).get("/api/v1/health").json()
    assert health["status"] == "unhealthy"
    assert health["graph_stats"] is None
