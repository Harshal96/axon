from __future__ import annotations

import pytest

from axon.v2.graph import GraphSnapshot
from axon.v2.models import GraphEdge, GraphNode, NodeKind

pytestmark = pytest.mark.tier1


def _snap(chain_len: int = 3) -> GraphSnapshot:
    """svc endpoint -> h0 -> h1 -> ... -> hN -CALLS_API-> target endpoint."""
    snap = GraphSnapshot()
    snap.add_node(
        GraphNode(
            id="service::r::svc",
            kind=NodeKind.SERVICE,
            name="svc",
            repo_id="r",
            service_id="svc",
        )
    )
    snap.add_node(
        GraphNode(id="ep", kind=NodeKind.API_ENDPOINT, name="GET /x", repo_id="r", service_id="svc")
    )
    snap.add_node(
        GraphNode(
            id="target", kind=NodeKind.API_ENDPOINT, name="GET /y", repo_id="r", service_id="svc"
        )
    )
    for i in range(chain_len):
        snap.add_node(GraphNode(id=f"h{i}", kind=NodeKind.SYMBOL, name=f"h{i}", repo_id="r"))
    snap.add_edge(GraphEdge(from_id="h0", to_id="ep", kind="HANDLES"))
    for i in range(chain_len - 1):
        snap.add_edge(GraphEdge(from_id=f"h{i}", to_id=f"h{i + 1}", kind="CALLS"))
    snap.add_edge(GraphEdge(from_id=f"h{chain_len - 1}", to_id="target", kind="CALLS_API"))
    return snap


def test_by_kind_and_edge_index_invalidate_on_add():
    snap = _snap()
    assert [t.id for _e, t in snap.outbound_calls("ep")] == ["target"]
    # Mutating edges must be visible without a new snapshot.
    snap.add_node(
        GraphNode(id="t2", kind=NodeKind.API_ENDPOINT, name="GET /z", repo_id="r", service_id="svc")
    )
    snap.add_edge(GraphEdge(from_id="h0", to_id="t2", kind="CALLS_API"))
    assert {t.id for _e, t in snap.outbound_calls("ep")} == {"target", "t2"}


def test_outbound_respects_depth_12_boundary():
    inside = _snap(chain_len=13)  # target caller at depth 12 from h0
    assert [t.id for _e, t in inside.outbound_calls("ep")] == ["target"]
    outside = _snap(chain_len=14)  # caller at depth 13: out of closure
    assert outside.outbound_calls("ep") == []


def test_cycles_do_not_hang_and_closure_is_memoized():
    snap = _snap(chain_len=3)
    snap.add_edge(GraphEdge(from_id="h2", to_id="h0", kind="CALLS"))
    assert [t.id for _e, t in snap.outbound_calls("ep")] == ["target"]
    depths = snap.call_closure("h0")
    assert depths == {"h0": 0, "h1": 1, "h2": 2}
    depths["junk"] = 99  # copy: must not poison the cache
    assert "junk" not in snap.call_closure("h0")


def test_endpoint_of_symbol_via_reverse_closure():
    snap = _snap(chain_len=4)
    assert [ep.id for ep in snap.endpoint_of_symbol("h3")] == ["ep"]
    assert snap.endpoint_of_symbol("missing") == []


def test_snapshot_has_no_whole_graph_service_map():
    snap = _snap(chain_len=2)
    assert not hasattr(snap, "service_map")
    # Cross-service direction is observed per API instead.
    assert [t.id for _e, t in snap.outbound_calls("ep")] == ["target"]


def test_graph_preserves_deep_outbound_paths_and_enforces_budget():
    snap = _snap(chain_len=9)
    for i in range(100):
        sid = f"noise{i:03}"
        snap.add_node(GraphNode(id=sid, kind=NodeKind.SYMBOL))
        snap.add_edge(GraphEdge(from_id="h0", to_id=sid, kind="CALLS"))
    graph = snap.api_graph("ep", max_symbols=10)
    ids = {n["id"] for n in graph["nodes"]}
    assert all(f"h{i}" in ids for i in range(9))
    assert "target" in ids
    assert sum(n["kind"] == "Symbol" for n in graph["nodes"]) <= 10
    assert graph["truncated"]
    assert graph["total_symbols"] == 109
    assert all(e["from_id"] in ids and e["to_id"] in ids for e in graph["edges"])


def test_graph_omits_outbound_when_whole_path_exceeds_budget():
    graph = _snap(chain_len=9).api_graph("ep", max_symbols=3)
    assert graph["truncated"]
    assert "target" not in {n["id"] for n in graph["nodes"]}
    assert len([n for n in graph["nodes"] if n["kind"] == "Symbol"]) == 3


def test_graph_bounds_callers_and_owner_fan_in():
    snap = _snap()
    for i in range(100):
        sid = f"caller{i:03}"
        snap.add_node(GraphNode(id=sid, kind=NodeKind.SYMBOL))
        snap.add_edge(GraphEdge(from_id=sid, to_id="ep", kind="CALLS_API"))
        for j in range(6):
            eid = f"owner{i:03}-{j}"
            snap.add_node(GraphNode(id=eid, kind=NodeKind.API_ENDPOINT))
            snap.add_edge(GraphEdge(from_id=sid, to_id=eid, kind="HANDLES"))
    graph = snap.api_graph("ep", max_callers=20, max_owners_per_caller=2)
    ids = {n["id"] for n in graph["nodes"]}
    assert len([sid for sid in ids if sid.startswith("caller")]) == 20
    assert len([sid for sid in ids if sid.startswith("owner")]) == 40
    assert graph["total_callers"] == 100
    assert graph["truncated"]


def test_all_handlers_contribute_to_traversal_and_graph():
    snap = _snap()
    snap.add_node(GraphNode(id="other", kind=NodeKind.SYMBOL))
    snap.add_node(GraphNode(id="remote", kind=NodeKind.API_ENDPOINT))
    snap.add_edge(GraphEdge(from_id="other", to_id="ep", kind="HANDLES"))
    snap.add_edge(GraphEdge(from_id="other", to_id="remote", kind="CALLS_API"))
    assert {t.id for _, t in snap.outbound_calls("ep")} == {"target", "remote"}
    assert {e.id for e in snap.endpoint_of_symbol("other")} == {"ep"}
    assert {h.id for h in snap.handlers_of("ep")} == {"h0", "other"}
    assert "other" in {n["id"] for n in snap.api_graph("ep")["nodes"]}


def test_graph_bounds_outbound_target_fan_out_and_is_repeatable():
    snap = _snap()
    for i in range(200):
        sid = f"remote{i:03}"
        snap.add_node(GraphNode(id=sid, kind=NodeKind.EXTERNAL_API))
        snap.add_edge(GraphEdge(from_id="h0", to_id=sid, kind="CALLS_API"))
    first = snap.api_graph("ep", max_symbols=5)
    assert first == snap.api_graph("ep", max_symbols=5)
    assert len([e for e in first["edges"] if e["kind"] == "CALLS_API"]) <= 5
    assert first["truncated"]


def test_generated_transport_does_not_inflate_service_outbound_coverage():
    snap = _snap()
    snap.nodes["h2"].service_id = "svc"
    snap.nodes["h2"].props["owner_kind"] = "client"
    assert snap.coverage_by_service("r")["svc"]["outbound_resolved"] == 0
