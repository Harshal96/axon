import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from axon.api.app import create_app
from axon.v1.graphql_inputs import graphql_dependency_paths
from axon.v1.incremental import index_and_publish
from axon.v1.indexer import IndexTimings, index_repo
from axon.v1.models import (
    Confidence,
    EdgeKind,
    NodeKind,
    endpoint_node_id,
    graphql_endpoint_node_id,
)
from axon.v1.scoped_index import endpoint_files
from axon.v1.store import MemoryStore
from tests.v1.test_api_fix import _wait_job
from tests.v1.test_incremental import canonical, commit, git

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/evals/typescript-apollo"
REPO = "eval-typescript-apollo"
ORDER = graphql_endpoint_node_id(REPO, "orders", "Query.order")
STATUS = graphql_endpoint_node_id(REPO, "orders", "Query.status")
VERSION = graphql_endpoint_node_id(REPO, "orders", "Query.version")
PAGE = endpoint_node_id(REPO, "shop", "GET", "/orders/{id}")
INVENTORY = endpoint_node_id(REPO, "inventory", "GET", "/orders/{id}")


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE, root)
    git(root, "init")
    commit(root)
    monkeypatch.setenv("AXON_INDEX_WORKERS", "1")
    monkeypatch.setenv("AXON_FACTS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AXON_STATE_DIR", str(tmp_path / "state"))
    return root


def call_edges(snapshot, caller):
    return [
        edge
        for edge in snapshot.edges
        if edge.kind == EdgeKind.CALLS_API and snapshot.nodes[edge.from_id].name == caller
    ]


def test_field_execution_and_declaration_evidence(checkout):
    snapshot, _ = index_repo(checkout, workers=1, use_cache=False)
    edges = call_edges(snapshot, "orderPage")
    assert sorted(edge.to_id for edge in edges) == sorted([ORDER, STATUS])
    assert [node.name for _, node in snapshot.inbound_callers(ORDER)] == ["orderPage"]
    for edge in edges:
        assert edge.confidence == Confidence.INFERRED
        assert edge.evidence.rule == "graphql:apollo-field"
        assert (edge.evidence.file, edge.evidence.line) == ("shop/app.ts", 10)
        assert edge.props["uri"] == "https://orders.example.test/graphql"
        assert edge.props["operation_name"] == "OrderPage"
        assert edge.props["operation_kind"] == "query"
        assert {"shop/orderDocument.ts", "shop/fragment.ts"} <= graphql_dependency_paths(edge)

    order_calls = [edge for edge in edges if edge.to_id == ORDER]
    selections = json.loads(order_calls[0].props["selections"])
    assert {selection["alias"] for selection in selections} == {"current", "repeated"}
    assert {selection["line"] for selection in selections} == {6, 7}
    status_call = next(edge for edge in edges if edge.to_id == STATUS)
    (status_selection,) = json.loads(status_call.props["selections"])
    assert (status_selection["file"], status_selection["line"]) == ("shop/fragment.ts", 5)

    definitions = [
        edge for edge in snapshot.edges if edge.kind == EdgeKind.DEFINES and edge.to_id == ORDER
    ]
    (declaration,) = definitions
    assert declaration.evidence.rule == "spec:graphql"
    assert snapshot.nodes[declaration.from_id].kind == NodeKind.API_SPEC
    assert (declaration.evidence.file, declaration.evidence.line) == ("orders/orders.graphql", 2)

    (resolver,) = snapshot.handlers_of(ORDER)
    assert resolver.name == "loadOrder" and resolver.file == "orders/orderResolver.ts"
    (registration,) = [
        edge for edge in snapshot.edges if edge.kind == EdgeKind.HANDLES and edge.to_id == ORDER
    ]
    assert registration.evidence.rule == "graphql:apollo-resolver"
    assert registration.confidence == Confidence.EXTRACTED
    assert registration.evidence.file == "orders/server.ts"
    (status_resolver,) = snapshot.handlers_of(STATUS)
    assert (status_resolver.file, status_resolver.line_start) == ("orders/server.ts", 14)
    assert [node.id for node in snapshot.outbound_endpoints(ORDER)] == [INVENTORY]
    assert snapshot.outbound_endpoints(STATUS) == []
    assert snapshot.handlers_of(VERSION) == []
    assert snapshot.inbound_callers(VERSION) == []


def test_calls_distinguish_invocation_columns_and_exclusions(checkout):
    snapshot, _ = index_repo(checkout, workers=1, use_cache=False)
    same_line = call_edges(snapshot, "pollTwice")
    assert [edge.to_id for edge in same_line] == [STATUS, STATUS]
    assert {edge.evidence.line for edge in same_line} == {7}
    assert len({int(edge.props["column"]) for edge in same_line}) == 2
    for name in ["fromCache", "shadowed", "clientLocal"]:
        assert not call_edges(snapshot, name)
    assert [edge.to_id for edge in call_edges(snapshot, "readAvailability")] == [STATUS]
    for name, reason in (("computed", "dynamic-document"), ("wrongPath", "unknown-target")):
        (edge,) = call_edges(snapshot, name)
        assert edge.evidence.rule == f"unresolved:graphql-{reason}"
        assert snapshot.nodes[edge.to_id].kind == NodeKind.EXTERNAL_API


def test_warm_cache_and_evidenced_scope_match_cold_build(checkout):
    cold, _ = index_repo(checkout, workers=1, use_cache=True)
    timings = IndexTimings()
    warm, _ = index_repo(checkout, workers=1, use_cache=True, timings=timings)
    assert timings.files_cached > 0 and timings.files_parsed == 0
    assert canonical(warm) == canonical(cold)
    assert {
        "orders/orders.graphql",
        "orders/server.ts",
        "orders/orderResolver.ts",
        "orders/inventory.ts",
        "shop/app.ts",
        "shop/client.ts",
        "shop/orderDocument.ts",
        "shop/fragment.ts",
    } <= endpoint_files(warm, ORDER)


@pytest.mark.parametrize(
    "edit", ["remove-field", "rename-resolver", "move-document", "edit-fragment", "remove-fragment"]
)
def test_incremental_reference_changes_equal_clean_publish(checkout, edit):
    root = checkout
    store = MemoryStore()
    first = git(root, "rev-parse", "HEAD")
    before, _, _ = index_and_publish(root, store, commit=first)
    assert {edge.to_id for edge in call_edges(before, "orderPage")} == {ORDER, STATUS}

    scope = {}
    if edit == "remove-field":
        path = root / "orders/orders.graphql"
        path.write_text(path.read_text().replace("  order(id: ID!): Order\n", ""))
        scope = {"services": ["orders"]}
    elif edit == "rename-resolver":
        path = root / "orders/orderResolver.ts"
        path.write_text(path.read_text().replace("function loadOrder(", "function readOrder("))
    elif edit == "move-document":
        (root / "shop/orderDocument.ts").rename(root / "shop/movedDocument.ts")
    elif edit == "edit-fragment":
        path = root / "shop/fragment.ts"
        path.write_text(path.read_text().replace("    status", "    version"))
        scope = {"api": "Query.order", "services": ["orders"]}
    else:
        (root / "shop/fragment.ts").unlink()
    revision = commit(root)
    assert not git(root, "diff", first, revision, "--", "shop/app.ts")
    updated, _, report = index_and_publish(root, store, commit=revision, **scope)
    assert report["mode"] == "incremental", report
    assert report["files_reused"] > 0
    clean, _ = index_repo(root, commit=revision, use_cache=False, workers=1)
    assert canonical(updated) == canonical(clean)
    assert canonical(store.load()) == canonical(clean)
    assert [edge.to_id for edge in call_edges(updated, "storeOpen")] == [STATUS]

    page_calls = call_edges(updated, "orderPage")
    if edit == "edit-fragment":
        assert {edge.to_id for edge in page_calls} == {ORDER, VERSION}
        assert report["api"] == ORDER
        assert [symbol["name"] for symbol in report["callers"]] == ["orderPage"]
    elif edit == "rename-resolver":
        assert updated.handlers_of(ORDER) == []
        assert updated.outbound_endpoints(ORDER) == []
        assert {edge.to_id for edge in page_calls} == {ORDER, STATUS}
    else:
        (edge,) = page_calls
        assert updated.nodes[edge.to_id].kind == NodeKind.EXTERNAL_API
        assert edge.evidence.rule.startswith("unresolved:graphql-")
        if edit == "remove-field":
            assert ORDER not in updated.nodes
            assert edge.evidence.rule == "unresolved:graphql-invalid-document"
            assert any(
                "shop/app.ts:10:" in site and edge.evidence.rule in site
                for site in updated.unresolved
            )
            assert any("shop/orderDocument.ts" in msg for msg in updated.diagnostics)
            assert set(report["coverage"]) == {"orders"}


def test_index_job_publishes_inspectable_field(checkout):
    with TestClient(create_app()) as client:
        created = client.post("/api/v1/index/jobs", json={"repo_path": str(checkout)})
        assert created.status_code == 202, created.text
        job = _wait_job(client, created.json()["job_id"])
        assert job["status"] == "done", job
        rows = client.get("/api/v1/apis", params={"service": "orders", "q": "Query.order"}).json()
        assert [row["id"] for row in rows] == [ORDER]
        detail = client.get(f"/api/v1/apis/{ORDER}").json()
        assert [node["name"] for node in detail["handlers"]] == ["loadOrder"]
        assert [node["id"] for node in detail["outbound"]] == [INVENTORY]
        assert [node["name"] for node in detail["callers"]] == ["orderPage"]
        page = client.get(f"/api/v1/apis/{PAGE}").json()
        assert {node["id"] for node in page["outbound"]} == {ORDER, STATUS}
