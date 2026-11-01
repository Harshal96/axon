"""Static function reach contracts independent of target repositories."""

import pytest

from axon.v2.functions import function_detail, package_of, query_functions
from axon.v2.graph import GraphSnapshot
from axon.v2.models import Confidence, EdgeKind, Evidence, GraphEdge, GraphNode, NodeKind

pytestmark = pytest.mark.tier1


def graph():
    snap = GraphSnapshot()
    for sid, owner, name in [
        ("ha", "alpha", "entry"),
        ("hb", "beta", "entry"),
        ("helper", "", "convert"),
        ("copy", "alpha", "convert"),
        ("unused", "alpha", "unused"),
        ("bridge", "alpha", "bridge"),
    ]:
        snap.add_node(
            GraphNode(
                id=sid,
                kind=NodeKind.SYMBOL,
                name=name,
                repo_id="sample",
                service_id=owner,
                file=f"{owner or 'lib'}/{name}.py",
                line_start=3,
                props={
                    "owner_kind": "service" if owner else "shared",
                    "owner_root": owner or "lib",
                    "kind": "function",
                    "language": "python",
                    "qualified": name,
                },
            )
        )
    for sid, svc in [("ea", "alpha"), ("eb", "beta")]:
        snap.add_node(
            GraphNode(
                id=sid,
                kind=NodeKind.API_ENDPOINT,
                repo_id="sample",
                service_id=svc,
                name=sid,
                props={"method": "GET", "path": "/"},
            )
        )
    for src, dst, kind, confidence in [
        ("ha", "ea", EdgeKind.HANDLES, Confidence.EXTRACTED),
        ("hb", "eb", EdgeKind.HANDLES, Confidence.EXTRACTED),
        ("ha", "bridge", EdgeKind.CALLS, Confidence.INFERRED),
        ("bridge", "helper", EdgeKind.CALLS, Confidence.AMBIGUOUS),
        ("hb", "helper", EdgeKind.CALLS, Confidence.EXTRACTED),
        ("ha", "copy", EdgeKind.CALLS, Confidence.EXTRACTED),
    ]:
        snap.add_edge(
            GraphEdge(
                from_id=src,
                to_id=dst,
                kind=kind,
                confidence=confidence,
                evidence=Evidence(file="alpha/entry.py", line=8, rule="call:source"),
            )
        )
    return snap


def test_usage_parity_and_invalidation():
    snap = graph()
    usage = snap.symbol_usage()
    for symbol in snap.by_kind(NodeKind.SYMBOL):
        assert {usage.endpoint_ids[i] for i in usage.eps.get(symbol.id, ())} == {
            ep.id for ep in snap.endpoint_of_symbol(symbol.id)
        }
    assert not snap._closure_cache
    snap.add_edge(GraphEdge(from_id="hb", to_id="unused", kind=EdgeKind.CALLS))
    assert snap.symbol_usage() is not usage
    assert len(snap.symbol_usage().eps["unused"]) == 1
    snap.add_node(GraphNode(id="ec", kind=NodeKind.API_ENDPOINT))
    assert "ec" in snap.symbol_usage().endpoint_ids


def test_ranking_filters_facets_and_paging():
    snap = graph()
    page = query_functions(snap)
    assert page["items"][0]["symbol"]["id"] == "helper"
    assert page["items"][0]["owner"]["key"] == "sample/shared/lib"
    assert page["items"][0]["same_name_count"] == 2
    assert page["items"][0]["service_count"] == 2
    assert all(not r["is_handler"] for r in page["items"])
    filtered = query_functions(snap, used_in=["sample/beta"])
    assert [r["symbol"]["id"] for r in filtered["items"]] == ["helper"]
    assert filtered["items"][0]["api_count"] == 1
    assert {f["key"] for f in filtered["facets"]["used_in"]} == {"sample/alpha", "sample/beta"}
    assert query_functions(snap, defined_in=["sample/shared/lib"])["total"] == 1
    assert query_functions(snap, repo="other")["total"] == 0
    assert query_functions(snap, kinds=["class"])["total"] == 0
    assert query_functions(snap, q="c")["total"] == page["total"]
    assert query_functions(snap, q="convert")["total"] == 2
    assert query_functions(snap, limit=1, offset=1)["items"] == page["items"][1:2]
    assert [r["symbol"]["id"] for r in query_functions(snap, unreached=True)["items"]] == ["unused"]


def test_detail_shortest_paths_confidence_and_evidence():
    detail = function_detail(graph(), "helper")
    paths = {a["endpoint"]["id"]: a for a in detail["apis"]}
    assert [n["id"] for n in paths["ea"]["path"]] == ["ha", "bridge", "helper"]
    assert paths["ea"]["depth"] == 2
    assert paths["ea"]["confidence"] == "AMBIGUOUS"
    assert paths["eb"]["confidence"] == "EXTRACTED"
    assert {c["symbol"]["id"] for c in detail["callers"]} == {"bridge", "hb"}
    assert detail["callers"][0]["evidence"]["rule"] == "call:source"
    with pytest.raises(KeyError):
        function_detail(graph(), "ea")


def test_depth_bound_cycles():
    snap = graph()
    previous = "helper"
    for i in range(13):
        current = f"chain{i}"
        snap.add_node(GraphNode(id=current, kind=NodeKind.SYMBOL, name=current))
        snap.add_edge(GraphEdge(from_id=previous, to_id=current, kind=EdgeKind.CALLS))
        previous = current
    snap.add_edge(GraphEdge(from_id="helper", to_id="ha", kind=EdgeKind.CALLS))
    usage = snap.symbol_usage()
    for sid in snap.nodes:
        assert {usage.endpoint_ids[i] for i in usage.eps.get(sid, ())} == {
            ep.id for ep in snap.endpoint_of_symbol(sid)
        }
    assert function_detail(snap, "chain12")["apis"] == []


def test_package_conventions():
    assert package_of("src/lib/core/__init__.py", "python") == "lib.core"
    assert package_of("src/main/java/org/sample/Thing.java", "java") == "org.sample"
    assert package_of("module/core/util.go", "go") == "module/core"


def test_function_routes_and_static_precedence():
    from fastapi.testclient import TestClient

    from axon.api.app import create_app
    from axon.v2.store import MemoryStore

    app = create_app()
    store = MemoryStore()
    store.save(graph())
    app.state.v2_store = store
    with TestClient(app) as client:
        response = client.get("/api/v2/functions")
        assert response.status_code == 200
        assert response.json()["items"][0]["symbol"]["id"] == "helper"
        legacy = client.get("/api/v2/functions/apis", params={"q": "convert"})
        assert legacy.status_code == 200
        assert len(legacy.json()) == 2
        assert {"symbol", "apis", "owner", "package"} <= legacy.json()[0].keys()
        assert client.get("/api/v2/functions/helper").json()["symbol"]["id"] == "helper"
        assert client.get("/api/v2/functions/ea").status_code == 404
        assert client.get("/api/v2/functions/missing").status_code == 404
        assert client.get("/api/v2/functions", params={"limit": 201}).status_code == 422
        scoped = client.get("/api/v2/functions", params={"used_in": "sample/beta"}).json()
        assert scoped["total"] == 1
        assert (
            client.get("/api/v2/functions/apis", params={"q": "convert", "repo": "other"}).json()
            == []
        )


def test_snapshot_copy_has_independent_lock_and_usage():
    snap = graph()
    snap.symbol_usage()
    copied = snap.model_copy(deep=True)
    assert copied._usage_lock is not snap._usage_lock
    copied.add_edge(GraphEdge(from_id="hb", to_id="unused", kind=EdgeKind.CALLS))
    assert copied.symbol_usage().eps.get("unused")
    assert not snap.symbol_usage().eps.get("unused")


def test_multiple_handlers_and_depth_twelve_boundary():
    snap = graph()
    snap.add_edge(GraphEdge(from_id="hb", to_id="ea", kind=EdgeKind.HANDLES))
    detail = function_detail(snap, "helper")
    path = next(item for item in detail["apis"] if item["endpoint"]["id"] == "ea")
    assert [node["id"] for node in path["path"]] == ["hb", "helper"]
    previous = "hb"
    for i in range(13):
        sid = f"depth{i + 1}"
        snap.add_node(GraphNode(id=sid, kind=NodeKind.SYMBOL))
        snap.add_edge(GraphEdge(from_id=previous, to_id=sid, kind=EdgeKind.CALLS))
        previous = sid
    assert function_detail(snap, "depth12")["apis"][0]["depth"] == 12
    assert function_detail(snap, "depth13")["apis"] == []
    assert snap.symbol_usage().eps["depth12"]
    assert not snap.symbol_usage().eps.get("depth13")


def test_facets_retain_alternatives_and_repo_identity():
    snap = graph()
    snap.add_node(
        GraphNode(
            id="foreign",
            kind=NodeKind.SYMBOL,
            repo_id="other",
            service_id="alpha",
            name="convert",
            props={"owner_kind": "service", "kind": "METHOD"},
        )
    )
    snap.add_node(
        GraphNode(id="foreign-api", kind=NodeKind.API_ENDPOINT, repo_id="other", service_id="alpha")
    )
    snap.add_edge(GraphEdge(from_id="foreign", to_id="foreign-api", kind=EdgeKind.HANDLES))
    page = query_functions(
        snap, hide_handlers=False, kinds=["method"], defined_in=["other/service/alpha"]
    )
    assert page["total"] == 1
    assert page["items"][0]["api_count"] == 1
    assert (
        query_functions(snap, repo="sample", used_in=["other/alpha"], hide_handlers=False)["total"]
        == 0
    )
    page = query_functions(snap, defined_in=["sample/shared/lib"])
    assert {facet["key"] for facet in page["facets"]["defined_in"]} == {
        "sample/shared/lib",
        "sample/service/alpha",
    }
    assert (
        query_functions(snap, hide_handlers=False, used_in=["other/alpha"])["items"][0]["symbol"][
            "id"
        ]
        == "foreign"
    )


def test_detail_definition_root_is_graph_evidence():
    snap = graph()
    assert function_detail(snap, "helper")["source_path"] == ""
    snap.add_node(
        GraphNode(
            id="repo::sample",
            kind=NodeKind.REPO,
            repo_id="sample",
            props={"source_path": "/sample/source"},
        )
    )
    assert function_detail(snap, "helper")["source_path"] == "/sample/source"


def test_scoped_minimum_depth_and_definition_count():
    page = query_functions(graph(), used_in=["sample/alpha"])
    helper = next(row for row in page["items"] if row["symbol"]["id"] == "helper")
    assert helper["min_depth"] == 2
    assert (
        next(row for row in query_functions(graph())["items"] if row["symbol"]["id"] == "helper")[
            "min_depth"
        ]
        == 1
    )
    shared = query_functions(graph(), defined_in=["sample/shared/lib"])["items"][0]
    assert shared["same_name_count"] == 2
