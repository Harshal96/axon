from __future__ import annotations

import threading

import pytest

from axon.v2.graph import GraphSnapshot
from axon.v2.graph_delta import GraphDelta
from axon.v2.models import Evidence, GraphEdge, GraphNode, NodeKind
from axon.v2.store import MemoryStore

pytestmark = pytest.mark.tier1


def node(name: str) -> GraphNode:
    return GraphNode(id=name, name=name, kind=NodeKind.SYMBOL, repo_id="repo")


def test_delta_preserves_evidence_and_old_reader_indexes():
    a, b = node("a"), node("b")
    old_edge = GraphEdge(from_id="a", to_id="b", kind="CALLS", evidence=Evidence(line=1))
    old = GraphSnapshot(nodes={"a": a, "b": b}, edges=[old_edge])
    assert old.call_closure("a") == {"a": 0, "b": 1}
    changed = old_edge.model_copy(deep=True)
    changed.evidence.detail = "source-visible binding"
    changed.props["rule_version"] = "2"
    target = GraphSnapshot(nodes={"a": a, "b": b}, edges=[changed], commit="new")
    store = MemoryStore(old)
    store.apply_delta(GraphDelta.between(old, target))
    assert store.load().model_dump() == target.model_dump()
    assert old.edges[0].evidence.detail == ""
    assert old.call_closure("a") == {"a": 0, "b": 1}
    assert store.load().nodes["a"] is a
    assert store.last_save_s >= 0


def test_delta_removes_incident_edges_and_keeps_unchanged_nodes():
    a, b = node("a"), node("b")
    old = GraphSnapshot(
        nodes={"a": a, "b": b}, edges=[GraphEdge(from_id="a", to_id="b", kind="CALLS")]
    )
    result = GraphDelta(remove_node_ids={"b"}).apply(old)
    assert result.edges == []
    assert result.nodes == {"a": a}
    assert len(old.edges) == 1


def test_concurrent_reader_observes_complete_publications():
    store = MemoryStore(GraphSnapshot(nodes={"a": node("a")}))
    old = store.load()
    done = threading.Event()
    errors: list[set[str]] = []

    def reader():
        while not done.is_set():
            ids = set(store.load().nodes)
            if ids not in ({"a"}, {"b", "c"}):
                errors.append(ids)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for _ in range(100):
            store.save(GraphSnapshot(nodes={"b": node("b"), "c": node("c")}))
            store.save(GraphSnapshot(nodes={"a": node("a")}))
    finally:
        done.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors
    assert set(old.nodes) == {"a"}


def test_repository_replace_prunes_only_unreferenced_external_nodes():
    from axon.v2.graph_delta import replace_repositories

    a = node("a")
    b = node("b").model_copy(update={"repo_id": "other"})
    external = GraphNode(id="external::GET:/items", kind=NodeKind.EXTERNAL_API)
    base = GraphSnapshot(
        nodes={n.id: n for n in (a, b, external)},
        edges=[GraphEdge(from_id=n.id, to_id=external.id, kind="CALLS_API") for n in (a, b)],
    )
    first = replace_repositories(base, GraphSnapshot(nodes={"a": a}))
    assert external.id in first.nodes
    assert len(first.edges) == 1 and first.edges[0].from_id == "b"
    second = replace_repositories(first, GraphSnapshot(nodes={"b": b}))
    assert external.id not in second.nodes
    assert second.edges == []
    assert external.id in base.nodes and len(base.edges) == 2
