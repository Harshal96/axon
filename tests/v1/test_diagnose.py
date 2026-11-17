import pytest

from axon.v1.diagnose import api_explain, client_show
from axon.v1.graph import GraphSnapshot
from axon.v1.models import EdgeKind, Evidence, GraphEdge, GraphNode, NodeKind


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

    from axon.v1.api import router
    from axon.v1.store import MemoryStore

    app = FastAPI()
    app.state.v1_store = MemoryStore()
    app.include_router(router, prefix="/api/v1")
    client = TestClient(app)
    assert client.get("/api/v1/repos/r/clients").json() == {"clients": []}
    assert client.get("/api/v1/repos/r/clients/missing").status_code == 404
    assert (
        client.get("/api/v1/repos/r/api-explain", params={"api": "GET /missing"}).status_code == 404
    )
    assert client.get("/api/v1/repos/r/refs/invalid/x").status_code == 400


def test_cli_diagnosis_registration():
    import re

    import typer
    from typer.testing import CliRunner

    from cli.commands.diagnose import register

    app = typer.Typer()
    register(app)
    result = CliRunner().invoke(app, ["api", "explain", "--help"])
    assert result.exit_code == 0
    # CI runners force ANSI color, which splits styled option names.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--local" in plain


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

    from axon.v1.api import router
    from axon.v1.store import MemoryStore

    app = FastAPI()
    app.state.v1_store = MemoryStore()
    app.include_router(router, prefix="/api/v1")
    endpoint = "endpoint::r::a::GET:/nested/path"
    response = TestClient(app).get("/api/v1/repos/r/refs/endpoint/" + quote(endpoint, safe=""))
    assert response.status_code == 200
    assert response.json()["entity"] == "endpoint:" + endpoint


def test_client_show_scopes_rules_by_source_service_and_requires_full_id():
    from tests.v1.test_ask_entities import qualified_snapshot

    snap = qualified_snapshot()
    for service in ("orders", "billing"):
        snap.add_node(
            GraphNode(
                id="client::A::" + service + "/sdk",
                kind=NodeKind.API_CLIENT,
                name="sdk",
                repo_id="A",
                service_id=service,
            )
        )
    snap.add_node(GraphNode(id="other", kind=NodeKind.SYMBOL, repo_id="A", service_id="billing"))
    snap.add_edge(
        GraphEdge(
            from_id="other",
            to_id="endpoint::C::ledger::GET:/run",
            kind=EdgeKind.CALLS_API,
            evidence=Evidence(rule="client:sdk"),
        )
    )
    report = client_show(snap, "A", "client::A::orders/sdk")
    assert report["linked"] == 2
    assert all(e["from_id"] == "A-handler" for e in report["edges"])
    assert client_show(snap, "A", "sdk")["status"] == "ambiguous"


@pytest.mark.parametrize(
    "repo,service,reason",
    [
        ("absent", "ledger", "target-repo-missing"),
        ("B", "absent", "target-service-missing"),
        ("B", "ledger", "target-endpoint-missing"),
    ],
)
def test_explain_classifies_scope_from_edge_not_shared_placeholder(repo, service, reason):
    from tests.v1.test_ask_entities import qualified_snapshot

    snap = qualified_snapshot()
    snap.add_node(
        GraphNode(
            id="shared",
            kind=NodeKind.EXTERNAL_API,
            props={"target_repo": "wrong", "target_service": "wrong"},
        )
    )
    snap.add_edge(
        GraphEdge(
            from_id="A-handler",
            to_id="shared",
            kind=EdgeKind.CALLS_API,
            props={
                "target_repo": repo,
                "target_service": service,
                "candidates": '["endpoint::B::ledger::GET:/run"]',
            },
        )
    )
    report = api_explain(snap, "A", "GET /run", "orders")
    row = report["unresolved"][0]
    assert row["reason"] == reason
    assert row["target_repo"] == repo and row["target_service"] == service
    assert row["candidate_nodes"][0]["repo_id"] == "B"
    assert report["publication_id"] == snap.publication_id


def test_explain_pinned_links_are_unknown_and_sdk_is_not_failed_link():
    from tests.v1.test_ask_entities import qualified_snapshot

    snap = qualified_snapshot()
    snap.edges[-1].props["relink_status"] = "pinned-legacy"
    snap.add_node(GraphNode(id="sdk", kind=NodeKind.EXTERNAL_API, props={"kind": "sdk"}))
    snap.add_edge(GraphEdge(from_id="A-handler", to_id="sdk", kind=EdgeKind.CALLS_API))
    report = api_explain(snap, "A", "GET /run", "orders")
    assert any("link-inputs-missing" in value for value in report["unknowns"])
    assert report["external_dependencies"][0]["target"]["id"] == "sdk"
    assert not report["unresolved"]


def test_base_suggestions_are_caller_scoped_and_qualified():
    from axon.v1.diagnose import base_url_suggestions
    from tests.v1.test_ask_entities import qualified_snapshot

    snap = qualified_snapshot()
    for edge in snap.edges:
        if edge.kind == EdgeKind.CALLS_API:
            edge.evidence.rule = "path-fallback:env:BASE"
            edge.props["base_ref"] = "env:BASE"
    # One caller has conflicting target scopes. A second caller's same base
    # must remain independently suggestible.
    snap.add_edge(
        GraphEdge(
            from_id="B-handler",
            to_id="endpoint::C::ledger::GET:/run",
            kind=EdgeKind.CALLS_API,
            evidence=Evidence(rule="path-fallback:env:BASE", file="app/main.py", line=2),
            props={"base_ref": "env:BASE"},
        )
    )
    assert base_url_suggestions(snap) == [
        {
            "repo_id": "B",
            "service_id": "ledger",
            "base_ref": "env:BASE",
            "target_repo": "C",
            "target_service": "ledger",
            "call_sites": ["app/main.py:2"],
        }
    ]


def test_legacy_without_retained_inputs_is_explicitly_unknown_in_consumers(tmp_path):
    from axon.v1.ask import ask_deterministic
    from axon.v1.doctor import doctor
    from tests.v1.test_ask_entities import qualified_snapshot

    snap = qualified_snapshot()
    (tmp_path / "axon.yaml").write_text("repo: A\nservices: [{id: orders}]\n")
    response = ask_deterministic(snap, "does A::orders call B::ledger?")
    assert any("link-inputs-missing" in spot for spot in response.blind_spots)
    report = api_explain(snap, "A", "GET /run", "orders")
    assert report["fresh_matching_unknown"]
    assert report["pinned_legacy"] == 2
    checked = doctor(tmp_path, snapshot=snap)
    assert any(issue["code"] == "link-inputs-missing" for issue in checked["issues"])


def test_pinned_sdk_stays_a_dependency_with_unknown_freshness():
    from tests.v1.test_ask_entities import qualified_snapshot

    snap = qualified_snapshot()
    snap.add_node(GraphNode(id="sdk", kind=NodeKind.EXTERNAL_API, props={"kind": "sdk"}))
    snap.add_edge(
        GraphEdge(
            from_id="A-handler",
            to_id="sdk",
            kind=EdgeKind.CALLS_API,
            props={"relink_status": "pinned-legacy"},
        )
    )
    report = api_explain(snap, "A", "GET /run", "orders")
    assert [row["target"]["id"] for row in report["external_dependencies"]] == ["sdk"]
    assert not report["unresolved"] and report["fresh_matching_unknown"]


def test_full_client_identity_with_slash_survives_server_routes():
    from urllib.parse import quote

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axon.v1.api import router
    from axon.v1.diagnose import client_list
    from axon.v1.store import MemoryStore
    from tests.v1.test_ask_entities import qualified_snapshot

    snap = qualified_snapshot()
    for service in ("orders", "billing"):
        snap.add_node(
            GraphNode(
                id=f"client::A::{service}/sdk",
                kind=NodeKind.API_CLIENT,
                repo_id="A",
                service_id=service,
                name="sdk",
            )
        )
    assert all("client" in row for row in client_list(snap, "A"))
    app = FastAPI()
    app.state.v1_store = MemoryStore(snap)
    app.include_router(router, prefix="/api/v1")
    response = TestClient(app).get(
        "/api/v1/repos/A/clients/" + quote("client::A::orders/sdk", safe="")
    )
    assert response.status_code == 200, response.text
    assert response.json()["client"]["service_id"] == "orders"
