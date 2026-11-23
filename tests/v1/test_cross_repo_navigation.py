from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from axon.api.app import create_app
from axon.v1.indexer import index_repo
from axon.v1.models import endpoint_node_id
from axon.v1.store import MemoryStore


def _publish(store: MemoryStore, checkout: Path, service: str, code: str) -> None:
    checkout.mkdir()
    (checkout / "axon.yaml").write_text(
        f"""version: 1
repo: {checkout.name}
services:
  - id: {service}
    roots: [app.py]
    language: python
    framework: fastapi
    base_urls: [https://{checkout.name}.example]
"""
    )
    (checkout / "app.py").write_text(
        "from fastapi import FastAPI\nimport requests\napp = FastAPI()\n" + code
    )
    snap, _ = index_repo(checkout, workers=1, use_cache=False)
    store.publish_repository(snap, repo_id=checkout.name)


def test_endpoint_view_opens_exact_handler_and_keeps_all_call_sites(tmp_path):
    store = MemoryStore()
    _publish(
        store,
        tmp_path / "orders",
        "orders",
        """
@app.get("/start")
def start():
    requests.get("https://finance.example/v1/graph")
    requests.get("https://finance.example/v1/graph")
    return {}
""",
    )
    _publish(
        store,
        tmp_path / "finance",
        "ledger",
        """
@app.get("/v1")
def version():
    return {}

@app.get("/v1/graph")
def entries():
    return {}
""",
    )
    app = create_app()
    app.state.v1_store = store
    caller_id = endpoint_node_id("orders", "orders", "GET", "/start")
    target_id = endpoint_node_id("finance", "ledger", "GET", "/v1/graph")
    with TestClient(app) as client:
        source = client.get("/api/v1/api-view", params={"endpoint_id": caller_id})
        assert source.status_code == 200, source.text
        assert [t["id"] for t in source.json()["detail"]["outbound"]] == [target_id]
        result = client.get("/api/v1/api-view", params={"endpoint_id": target_id})
        assert result.status_code == 200, result.text
        page = result.json()
        assert page["graph"]["publication_id"] == page["detail"]["publication_id"]
        assert page["detail"]["handler"]["name"] == "entries"
        assert page["detail"]["handler_source_path"] == str(tmp_path / "finance")
        callers = page["detail"]["callers"]
        assert len(callers) == 1 and callers[0]["repo_id"] == "orders"
        sites = page["detail"]["caller_sites"]
        assert [site["evidence"]["line"] for site in sites] == [7, 8]
        graph = client.get("/api/v1/api-graph", params={"endpoint_id": target_id})
        assert graph.status_code == 200, graph.text
        edge = next(e for e in graph.json()["edges"] if e["kind"] == "CALLS_API")
        assert [site["evidence"]["line"] for site in edge["sites"]] == [7, 8]
        legacy_graph = client.get(f"/api/v1/apis/{quote(target_id, safe='')}/graph")
        assert legacy_graph.json()["endpoint"]["id"] == target_id
        assert client.delete("/api/v1/repos/finance").status_code == 200
        missing = client.get("/api/v1/api-view", params={"endpoint_id": target_id})
        assert missing.status_code == 404
        source = client.get("/api/v1/api-view", params={"endpoint_id": caller_id}).json()
        assert source["detail"]["outbound"][0]["kind"] == "ExternalApi"
        assert source["detail"]["outbound_sites"][0]["reason"] != "resolved"
