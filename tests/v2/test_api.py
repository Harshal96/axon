import pytest
from fastapi.testclient import TestClient

from axon.api.app import create_app

pytestmark = pytest.mark.tier1


@pytest.fixture()
def client(fixtures):
    app = create_app()
    with TestClient(app) as c:
        resp = c.post("/api/v2/index", json={"repo_path": str(fixtures / "monorepo")})
        assert resp.status_code == 200, resp.text
        yield c


def test_index_response_has_timings(fixtures):
    from axon.api.app import create_app

    with TestClient(create_app()) as c:
        resp = c.post("/api/v2/index", json={"repo_path": str(fixtures / "monorepo")})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["timings"]["files_total"] > 0
        assert body["timings"]["total_s"] > 0
        assert body["parse_warnings"] == body["timings"]["parse_warnings"]
        assert isinstance(body["parse_warning_samples"], list)


def test_index_job_completes_and_publishes(fixtures):
    import time

    from axon.api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        created = c.post("/api/v2/index/jobs", json={"repo_path": str(fixtures / "monorepo")})
        assert created.status_code == 202, created.text
        job_id = created.json()["job_id"]
        job = {}
        for _ in range(200):
            job = c.get(f"/api/v2/index/jobs/{job_id}").json()
            if job["status"] in ("done", "error", "cancelled"):
                break
            time.sleep(0.05)
        assert job["status"] == "done", job
        assert job["result"]["repo"]
        assert job["timings"]["files_total"] > 0
        apis = c.get("/api/v2/apis").json()
        assert apis
        assert c.get("/api/v2/index/jobs/nope").status_code == 404
        cancelled = c.post(f"/api/v2/index/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200


def test_index_skips_already_indexed_commit(fixtures):
    from axon.api.app import create_app

    with TestClient(create_app()) as c:
        body = {"repo_path": str(fixtures / "monorepo"), "commit": "abc123"}
        first = c.post("/api/v2/index", json=body)
        assert first.status_code == 200, first.text
        assert "already_indexed" not in first.json()
        second = c.post("/api/v2/index", json=body)
        assert second.json()["already_indexed"] is True
        forced = c.post("/api/v2/index", json={**body, "force": True})
        assert "already_indexed" not in forced.json()
        other = c.post("/api/v2/index", json={**body, "commit": "def456"})
        assert "already_indexed" not in other.json()


def test_scan_job_completes_with_progress(fixtures):
    import time

    from axon.api.app import create_app

    with TestClient(create_app()) as c:
        created = c.post(
            "/api/v2/onboard/scan/jobs", json={"repo_path": str(fixtures / "monorepo")}
        )
        assert created.status_code == 202, created.text
        job_id = created.json()["job_id"]
        job = {}
        for _ in range(200):
            job = c.get(f"/api/v2/jobs/{job_id}").json()
            if job["status"] in ("done", "error", "cancelled"):
                break
            time.sleep(0.05)
        assert job["status"] == "done", job
        assert job["kind"] == "scan"
        assert "draft_yaml" in (job["result"] or {})
        assert c.get("/api/v2/jobs/nope").status_code == 404


def test_prewarm_store_swallows_failures():
    from axon.api.app import _prewarm_store
    from axon.v2.graph import GraphSnapshot

    class Good:
        def load(self):
            return GraphSnapshot()

    class Bad:
        def load(self):
            raise RuntimeError("db down")

    _prewarm_store(Good())  # type: ignore[arg-type]
    _prewarm_store(Bad())  # type: ignore[arg-type]  # must not raise


def test_services_and_apis(client):
    services = client.get("/api/v2/services").json()
    assert {s["service_id"] for s in services} == {"payment", "checkout"}
    apis = client.get("/api/v2/apis", params={"service": "payment"}).json()
    paths = {a["props"]["path"] for a in apis}
    assert "/v1/charges" in paths


def test_api_detail_and_graph(client):
    apis = client.get("/api/v2/apis", params={"q": "/checkout"}).json()
    checkout = next(a for a in apis if a["props"]["path"] == "/checkout")
    detail = client.get(f"/api/v2/apis/{checkout['id']}").json()
    assert detail["handler"]["name"] == "place_order"
    assert any(o["props"]["path"] == "/v1/charges" for o in detail["outbound"])
    graph = client.get(f"/api/v2/apis/{checkout['id']}/graph").json()
    kinds = {n["kind"] for n in graph["nodes"]}
    assert "ApiEndpoint" in kinds and "Symbol" in kinds


def test_service_map_endpoint_is_removed(client):
    # The whole-graph overview was removed: exploration is per-API now.
    assert client.get("/api/v2/service-map").status_code == 404
    # The per-API graph for checkout still carries call direction.
    apis = client.get("/api/v2/apis", params={"q": "/checkout"}).json()
    checkout = next(a for a in apis if a["props"]["path"] == "/checkout")
    graph = client.get(f"/api/v2/apis/{checkout['id']}/graph").json()
    paths = {n["props"].get("path") for n in graph["nodes"]}
    assert "/v1/charges" in paths


def test_function_apis_and_ask(client):
    rows = client.get("/api/v2/functions/apis", params={"q": "price_cart"}).json()
    assert rows and rows[0]["apis"][0]["props"]["path"] == "/checkout"
    resp = client.post("/api/v2/ask", json={"question": "Can GET /v1/legacy be deprecated?"}).json()
    assert resp["intent"] == "deprecatable"
    assert resp["citations"]


def test_impact_endpoint(client, fixtures):
    resp = client.post(
        "/api/v2/impact",
        json={
            "repo_path": str(fixtures / "monorepo"),
            "changed_files": ["shared/util.py"],
        },
    ).json()
    assert any("/checkout" in a for a in resp["affected_apis"])
    assert "Axon API impact" in resp["markdown"]


def test_graph_budget_query_parameters_and_metadata(client):
    endpoints = client.get("/api/v2/apis").json()
    endpoint_id = endpoints[0]["id"]
    response = client.get(
        f"/api/v2/apis/{endpoint_id}/graph",
        params={"max_symbols": 1, "max_callers": 0, "max_owners_per_caller": 0},
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["truncated"], bool)
    assert body["total_symbols"] >= 0
    assert body["total_callers"] >= 0
    ids = {n["id"] for n in body["nodes"]}
    assert all(e["from_id"] in ids and e["to_id"] in ids for e in body["edges"])
    assert len([n for n in body["nodes"] if n["kind"] == "Symbol"]) <= 1
    detail = client.get(f"/api/v2/apis/{endpoint_id}").json()
    assert isinstance(detail["handlers"], list)
