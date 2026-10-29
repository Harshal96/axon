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
