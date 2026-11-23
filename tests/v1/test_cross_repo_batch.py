"""Publication contracts for mutually dependent repositories."""

from pathlib import Path

from fastapi.testclient import TestClient

from axon.api.app import create_app
from axon.v1.indexer import index_repo
from axon.v1.models import endpoint_node_id


def _repo(parent: Path, name: str, target: str) -> Path:
    root = parent / name
    root.mkdir()
    (root / "axon.yaml").write_text(
        f"repo: {name}\nservices:\n- id: api\n  roots: ['.']\n  base_urls: [http://{name}.example]\n"
    )
    (root / "app.py").write_text(
        "from fastapi import FastAPI\nimport requests\napp = FastAPI()\n"
        "@app.get('/ping')\ndef ping():\n"
        f"    return requests.get('http://{target}.example/ping').json()\n"
    )
    (root / "axon.golden.yaml").write_text(
        "version: 1\ncases:\n"
        "- api: {service: api, method: GET, path: /ping}\n"
        f"  expect: [{{repo: {target}, service: api, method: GET, path: /ping}}]\n"
    )
    return root


def test_bootstrap_mutual_contracts_and_verify_shared_graph(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_INDEX_WORKERS", "1")
    a = _repo(tmp_path, "a", "b")
    b = _repo(tmp_path, "b", "a")
    with TestClient(create_app()) as client:
        for root in (a, b):
            assert client.post("/api/v1/index", json={"repo_path": str(root)}).status_code == 400
        result = client.post("/api/v1/index/batch", json={"repo_paths": [str(a), str(b)]})
        assert result.status_code == 200, result.text
        assert result.json()["repositories"] == ["a", "b"]
        verified = client.post("/api/v1/graph/verify", json={"repo_id": "a"})
        assert verified.status_code == 200, verified.text
        assert verified.json()["ok"]
        assert verified.json()["publication_id"] == result.json()["publication_id"]


def test_bad_batch_contract_leaves_both_repositories_unpublished(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_INDEX_WORKERS", "1")
    a = _repo(tmp_path, "a", "b")
    b = _repo(tmp_path, "b", "c")
    with TestClient(create_app()) as client:
        rejected = client.post("/api/v1/index/batch", json={"repo_paths": [str(a), str(b)]})
        assert rejected.status_code == 400, rejected.text
        assert "missing expected target" in rejected.json()["detail"]
        assert client.get("/api/v1/repos").json() == []


def test_target_update_reports_links_lost_by_other_repositories(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_INDEX_WORKERS", "1")
    a = _repo(tmp_path, "a", "b")
    b = _repo(tmp_path, "b", "a")
    (b / "axon.golden.yaml").unlink()
    with TestClient(create_app()) as client:
        for root in (b, a):
            result = client.post("/api/v1/index", json={"repo_path": str(root)})
            assert result.status_code == 200, result.text
        source = b / "app.py"
        source.write_text(source.read_text().replace("@app.get('/ping')", "@app.get('/new')"))
        updated = client.post("/api/v1/index", json={"repo_path": str(b)})
        assert updated.status_code == 200, updated.text
        lost = updated.json()["lost_cross_repo_links"]
        assert len(lost) == 1
        assert lost[0]["caller_repo"] == "a"
        assert lost[0]["target_id"] == endpoint_node_id("b", "api", "GET", "/ping")
        assert not client.post("/api/v1/graph/verify", json={"repo_id": "a"}).json()["ok"]


def test_snapshot_import_updates_the_serving_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_INDEX_WORKERS", "1")
    a = _repo(tmp_path, "a", "b")
    b = _repo(tmp_path, "b", "a")
    (a / "axon.golden.yaml").unlink()
    snapshot, _ = index_repo(b)
    with TestClient(create_app()) as client:
        assert client.post("/api/v1/index", json={"repo_path": str(a)}).status_code == 200
        endpoint = endpoint_node_id("a", "api", "GET", "/ping")
        params = {"endpoint_id": endpoint}
        before = client.get("/api/v1/api-view", params=params).json()["detail"]
        assert before["outbound"][0]["kind"] == "ExternalApi"
        imported = client.post("/api/v1/graph/import", json=snapshot.model_dump(mode="json"))
        assert imported.status_code == 200, imported.text
        after = client.get("/api/v1/api-view", params=params).json()["detail"]
        assert after["outbound"][0]["id"] == endpoint_node_id("b", "api", "GET", "/ping")
        assert after["publication_id"] == imported.json()["publication_id"]
