import pytest
from fastapi.testclient import TestClient

from axon.api.app import create_app

pytestmark = pytest.mark.tier1


@pytest.fixture()
def client(fixtures):
    app = create_app()
    with TestClient(app) as c:
        resp = c.post("/api/v1/index", json={"repo_path": str(fixtures / "monorepo")})
        assert resp.status_code == 200, resp.text
        yield c


def test_index_response_has_timings(fixtures):
    from axon.api.app import create_app

    with TestClient(create_app()) as c:
        resp = c.post("/api/v1/index", json={"repo_path": str(fixtures / "monorepo")})
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
        created = c.post("/api/v1/index/jobs", json={"repo_path": str(fixtures / "monorepo")})
        assert created.status_code == 202, created.text
        job_id = created.json()["job_id"]
        job = {}
        for _ in range(200):
            job = c.get(f"/api/v1/index/jobs/{job_id}").json()
            if job["status"] in ("done", "error", "cancelled"):
                break
            time.sleep(0.05)
        assert job["status"] == "done", job
        assert job["result"]["repo"]
        assert job["timings"]["files_total"] > 0
        apis = c.get("/api/v1/apis").json()
        assert apis
        assert c.get("/api/v1/index/jobs/nope").status_code == 404
        cancelled = c.post(f"/api/v1/index/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200


def test_index_skips_already_indexed_commit(fixtures, monkeypatch, tmp_path):
    from axon.api.app import create_app

    for module in ["revision", "indexer", "incremental"]:
        monkeypatch.setattr(f"axon.v1.{module}.checkout_dirty", lambda root: False)
    monkeypatch.setenv("AXON_STATE_DIR", str(tmp_path / "state"))
    with TestClient(create_app()) as c:
        body = {"repo_path": str(fixtures / "monorepo"), "commit": "abc123"}
        first = c.post("/api/v1/index", json=body)
        assert first.status_code == 200, first.text
        assert "already_indexed" not in first.json()
        second = c.post("/api/v1/index", json=body)
        assert second.json()["already_indexed"] is True
        forced = c.post("/api/v1/index", json={**body, "force": True})
        assert "already_indexed" not in forced.json()
        other = c.post("/api/v1/index", json={**body, "commit": "def456"})
        assert "already_indexed" not in other.json()


def test_scan_job_completes_with_progress(fixtures):
    import time

    from axon.api.app import create_app

    with TestClient(create_app()) as c:
        created = c.post(
            "/api/v1/onboard/scan/jobs", json={"repo_path": str(fixtures / "monorepo")}
        )
        assert created.status_code == 202, created.text
        job_id = created.json()["job_id"]
        job = {}
        for _ in range(200):
            job = c.get(f"/api/v1/jobs/{job_id}").json()
            if job["status"] in ("done", "error", "cancelled"):
                break
            time.sleep(0.05)
        assert job["status"] == "done", job
        assert job["kind"] == "scan"
        assert "draft_yaml" in (job["result"] or {})
        assert c.get("/api/v1/jobs/nope").status_code == 404


def test_prewarm_store_swallows_failures():
    from axon.api.app import _prewarm_store
    from axon.v1.graph import GraphSnapshot

    class Good:
        def load(self):
            return GraphSnapshot()

    class Bad:
        def load(self):
            raise RuntimeError("db down")

    _prewarm_store(Good())  # type: ignore[arg-type]
    _prewarm_store(Bad())  # type: ignore[arg-type]  # must not raise


def test_services_and_apis(client):
    services = client.get("/api/v1/services").json()
    assert {s["service_id"] for s in services} == {"payment", "checkout"}
    apis = client.get("/api/v1/apis", params={"service": "payment"}).json()
    paths = {a["props"]["path"] for a in apis}
    assert "/v1/charges" in paths


def test_api_detail_and_graph(client):
    apis = client.get("/api/v1/apis", params={"q": "/checkout"}).json()
    checkout = next(a for a in apis if a["props"]["path"] == "/checkout")
    detail = client.get(f"/api/v1/apis/{checkout['id']}").json()
    assert detail["handler"]["name"] == "place_order"
    assert any(o["props"]["path"] == "/v1/charges" for o in detail["outbound"])
    graph = client.get(f"/api/v1/apis/{checkout['id']}/graph").json()
    kinds = {n["kind"] for n in graph["nodes"]}
    assert "ApiEndpoint" in kinds and "Symbol" in kinds


def test_service_map_endpoint_is_removed(client):
    # The whole-graph overview was removed: exploration is per-API now.
    assert client.get("/api/v1/service-map").status_code == 404
    # The per-API graph for checkout still carries call direction.
    apis = client.get("/api/v1/apis", params={"q": "/checkout"}).json()
    checkout = next(a for a in apis if a["props"]["path"] == "/checkout")
    graph = client.get(f"/api/v1/apis/{checkout['id']}/graph").json()
    paths = {n["props"].get("path") for n in graph["nodes"]}
    assert "/v1/charges" in paths


def test_function_apis_and_ask(client):
    rows = client.get("/api/v1/functions/apis", params={"q": "price_cart"}).json()
    assert rows and rows[0]["apis"][0]["props"]["path"] == "/checkout"
    resp = client.post("/api/v1/ask", json={"question": "Can GET /v1/legacy be deprecated?"}).json()
    assert resp["intent"] == "deprecatable"
    assert resp["citations"]


def test_impact_endpoint(client, fixtures):
    resp = client.post(
        "/api/v1/impact",
        json={
            "repo_path": str(fixtures / "monorepo"),
            "changed_files": ["shared/util.py"],
        },
    ).json()
    assert any("/checkout" in a for a in resp["affected_apis"])
    assert "Axon API impact" in resp["markdown"]


def test_graph_budget_query_parameters_and_metadata(client):
    endpoints = client.get("/api/v1/apis").json()
    endpoint_id = endpoints[0]["id"]
    response = client.get(
        f"/api/v1/apis/{endpoint_id}/graph",
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
    detail = client.get(f"/api/v1/apis/{endpoint_id}").json()
    assert isinstance(detail["handlers"], list)


def test_lightweight_health_reports_warmup_without_loading_store():
    from fastapi.testclient import TestClient

    from axon.api.app import create_app

    app = create_app()
    app.state.warming = True
    app.state.warmup_progress = {"phase": "load", "done": 0, "total": 1}
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["warming"] is True
    assert response.json()["progress"]["phase"] == "load"


def test_health_serves_while_job_registry_lock_is_busy(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from axon.v1 import api
    from axon.v1.jobs import JobRegistry

    registry = JobRegistry()
    job = registry.create("/repo")
    waiting = threading.Event()
    original = registry.snapshot

    def snapshot(job_id, **kwargs):
        waiting.set()
        return original(job_id, **kwargs)

    monkeypatch.setattr(registry, "snapshot", snapshot)
    monkeypatch.setattr(api, "JOBS", registry)
    with TestClient(create_app()) as client, ThreadPoolExecutor() as pool:
        registry.lock.acquire()
        try:
            pending = pool.submit(client.get, f"/api/v1/jobs/{job.id}")
            assert waiting.wait(2)
            health = pool.submit(client.get, "/health")
            assert health.result(timeout=2).status_code == 200
        finally:
            registry.lock.release()
        assert pending.result(timeout=2).status_code == 200


def test_server_scan_never_writes_config(tmp_path):
    from fastapi.testclient import TestClient

    from axon.api.app import app

    with TestClient(app) as client:
        for route in ("/api/v1/onboard/scan", "/api/v1/onboard/scan/jobs"):
            response = client.post(route, json={"repo_path": str(tmp_path), "write": True})
            assert response.status_code == 400
    assert not (tmp_path / "axon.yaml").exists()


def test_repository_source_conflict_and_removal(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_STATE_DIR", str(tmp_path / "state"))
    import shutil

    from tests.v1.test_incremental import remote_repos

    a, b = remote_repos(tmp_path)
    other = tmp_path / "other"
    shutil.copytree(a, other)
    with TestClient(create_app()) as client:
        for root in [a, b]:
            response = client.post("/api/v1/index", json={"repo_path": str(root)})
            assert response.status_code == 200, response.text
        response = client.post("/api/v1/index", json={"repo_path": str(other)})
        assert response.status_code == 409, response.text
        response = client.post("/api/v1/index/jobs", json={"repo_path": str(other)})
        assert response.status_code == 409, response.text
        response = client.post(
            "/api/v1/index", json={"repo_path": str(other), "replace_source": True}
        )
        assert response.status_code == 200, response.text
        shutil.rmtree(a)
        shutil.rmtree(b)
        removed = client.delete("/api/v1/repos/B")
        assert removed.status_code == 200, removed.text
        assert removed.json()["repo"] == "B"
        assert removed.json()["publication_id"]
        assert (
            removed.json()["affected_callers"]["A"]["coverage"]["orders"]["outbound_external"] == 1
        )


@pytest.mark.parametrize("repo_index", [0, 1])
def test_same_commit_legacy_missing_inputs_does_not_skip(tmp_path, monkeypatch, repo_index):
    monkeypatch.setenv("AXON_STATE_DIR", str(tmp_path / "state"))
    from axon.v1.store import MemoryStore
    from tests.v1.test_incremental import remote_repos

    a = remote_repos(tmp_path)[repo_index]
    with TestClient(create_app()) as client:
        assert client.post("/api/v1/index", json={"repo_path": str(a)}).status_code == 200
        snap = client.app.state.v1_store.load().model_copy(deep=True)
        snap.link_inputs = {}
        snap.link_reports = {}
        client.app.state.v1_store = MemoryStore(snap)
        response = client.post("/api/v1/index", json={"repo_path": str(a)})
        assert response.status_code == 200, response.text
        assert not response.json().get("already_indexed")


def test_same_commit_caller_skip_uses_latest_target_report(tmp_path, monkeypatch):
    from tests.v1.test_incremental import remote_repos

    monkeypatch.setenv("AXON_STATE_DIR", str(tmp_path / "state"))
    a, b = remote_repos(tmp_path)
    with TestClient(create_app()) as client:
        for root in [a, b]:
            response = client.post("/api/v1/index", json={"repo_path": str(root)})
            assert response.status_code == 200, response.text
        response = client.post("/api/v1/index", json={"repo_path": str(a)})
        result = response.json()
        assert result["already_indexed"]
        assert result["coverage"]["orders"]["outbound_resolved"] == 1
        assert result["unresolved"] == []
        assert result["publication_id"] == client.app.state.v1_store.load().publication_id
        assert result["relinked_repos"] == ["A", "B"]


def test_job_stays_done_when_postcommit_progress_observer_fails(tmp_path, monkeypatch):
    import time

    from axon.v1.jobs import JOBS
    from tests.v1.test_incremental import remote_repos

    monkeypatch.setenv("AXON_STATE_DIR", str(tmp_path / "state"))
    a, _ = remote_repos(tmp_path)
    app = create_app()
    update = JOBS.update

    def observer(job_id, **fields):
        if app.state.v1_store.load().publication_id and "status" not in fields:
            from axon.v1.indexer import IndexCancelled

            raise IndexCancelled()
        return update(job_id, **fields)

    monkeypatch.setattr(JOBS, "update", observer)
    with TestClient(app) as client:
        created = client.post("/api/v1/index/jobs", json={"repo_path": str(a)})
        assert created.status_code == 202, created.text
        for _ in range(100):
            job = client.get("/api/v1/index/jobs/" + created.json()["job_id"]).json()
            if job["status"] in {"done", "cancelled", "error"}:
                break
            time.sleep(0.01)
        assert job["status"] == "done", job
        assert job["result"]["publication_id"] == app.state.v1_store.load().publication_id
        assert job["result"]["warnings"]


def test_remove_accepts_slash_repo_identity(tmp_path, monkeypatch):
    from urllib.parse import quote

    monkeypatch.setenv("AXON_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "axon.yaml").write_text("repo: acme/service\nservices: []\n")
    with TestClient(create_app()) as client:
        response = client.post("/api/v1/index", json={"repo_path": str(tmp_path)})
        assert response.status_code == 200, response.text
        removed = client.delete("/api/v1/repos/" + quote("acme/service", safe=""))
        assert removed.status_code == 200, removed.text
        assert removed.json()["repo"] == "acme/service"
        assert removed.json()["publication_id"] == client.app.state.v1_store.load().publication_id
        assert client.get("/api/v1/repos").json() == []
