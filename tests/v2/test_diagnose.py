import pytest

from axon.v2.diagnose import api_explain, client_show
from axon.v2.graph import GraphSnapshot
from axon.v2.models import EdgeKind, Evidence, GraphEdge, GraphNode, NodeKind


def test_explain_only_reachable_graph_evidence():
    snap = GraphSnapshot()
    for node in [
        GraphNode(
            id="ep",
            kind=NodeKind.API_ENDPOINT,
            repo_id="r",
            service_id="a",
            props={"method": "GET", "path": "/a"},
        ),
        GraphNode(id="handler", kind=NodeKind.SYMBOL, repo_id="r", service_id="a"),
        GraphNode(id="ext", kind=NodeKind.EXTERNAL_API, repo_id="r"),
    ]:
        snap.add_node(node)
    snap.add_edge(GraphEdge(from_id="handler", to_id="ep", kind=EdgeKind.HANDLES))
    snap.add_edge(
        GraphEdge(
            from_id="handler",
            to_id="ext",
            kind=EdgeKind.CALLS_API,
            evidence=Evidence(file="a.py", line=4, rule="unresolved:no-client"),
        )
    )
    report = api_explain(snap, "r", "GET /a", "a")
    assert report["handlers"][0]["id"] == "handler"
    assert report["unresolved"][0]["evidence"]["line"] == 4
    assert report["unresolved"][0]["suggested_fix"] == "declare-client"
    assert report["outbound"] == []


def test_unknown_endpoint_and_client():
    with pytest.raises(KeyError):
        api_explain(GraphSnapshot(), "r", "GET /missing", "a")
    with pytest.raises(KeyError):
        client_show(GraphSnapshot(), "r", "missing")


def test_readonly_server_routes():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axon.v2.api import router
    from axon.v2.store import MemoryStore

    app = FastAPI()
    app.state.v2_store = MemoryStore()
    app.include_router(router, prefix="/api/v2")
    client = TestClient(app)
    assert client.get("/api/v2/repos/r/clients").json() == {"clients": []}
    assert client.get("/api/v2/repos/r/clients/missing").status_code == 404
    assert (
        client.get("/api/v2/repos/r/api-explain", params={"api": "GET /missing"}).status_code == 404
    )
    assert client.get("/api/v2/repos/r/refs/invalid/x").status_code == 400


def test_cli_diagnosis_registration():
    import typer
    from typer.testing import CliRunner

    from cli.commands.diagnose import register

    app = typer.Typer()
    register(app)
    result = CliRunner().invoke(app, ["api", "explain", "--help"])
    assert result.exit_code == 0
    assert "--local" in result.output


def test_explain_preserves_candidate_rows():
    snap = GraphSnapshot()
    snap.add_node(
        GraphNode(
            id="ep",
            kind=NodeKind.API_ENDPOINT,
            repo_id="r",
            service_id="a",
            props={"method": "GET", "path": "/a"},
        )
    )
    snap.add_node(GraphNode(id="handler", kind=NodeKind.SYMBOL, repo_id="r"))
    snap.add_node(GraphNode(id="ext", kind=NodeKind.EXTERNAL_API, repo_id="r"))
    snap.add_edge(GraphEdge(from_id="handler", to_id="ep", kind=EdgeKind.HANDLES))
    snap.add_edge(
        GraphEdge(
            from_id="handler",
            to_id="ext",
            kind=EdgeKind.CALLS_API,
            props={"candidates": '["endpoint::r::b::GET:/b", "missing-id"]'},
        )
    )
    report = api_explain(snap, "r", "GET /a", "a")
    assert report["unresolved"][0]["candidates"] == ["endpoint::r::b::GET:/b", "missing-id"]
    snap.edges[-1].props["candidates"] = "invalid JSON"
    assert api_explain(snap, "r", "GET /a", "a")["unresolved"][0]["candidates"] == []


def test_remote_endpoint_refs_accept_path_slashes():
    from urllib.parse import quote

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axon.v2.api import router
    from axon.v2.store import MemoryStore

    app = FastAPI()
    app.state.v2_store = MemoryStore()
    app.include_router(router, prefix="/api/v2")
    endpoint = "endpoint::r::a::GET:/nested/path"
    response = TestClient(app).get("/api/v2/repos/r/refs/endpoint/" + quote(endpoint, safe=""))
    assert response.status_code == 200
    assert response.json()["entity"] == "endpoint:" + endpoint
