from __future__ import annotations

import pytest

from axon.v2.graph import GraphSnapshot
from axon.v2.models import GraphEdge, GraphNode, NodeKind
from axon.v2.store import NEO4J_BATCH_SIZE, MemoryStore, Neo4jStore

pytestmark = pytest.mark.tier1


def _node(nid: str, repo: str = "r") -> GraphNode:
    return GraphNode(id=nid, kind=NodeKind.SYMBOL, name=nid, repo_id=repo)


def test_memory_store_save_replaces_repo_without_copy():
    store = MemoryStore()
    first = GraphSnapshot()
    a = _node("a")
    first.add_node(a)
    store.save(first)

    second = GraphSnapshot()
    b = _node("b")
    second.add_node(b)
    second.add_edge(GraphEdge(from_id="b", to_id="b", kind="CALLS"))
    store.save(second)

    assert set(store.snap.nodes) == {"b"}
    assert store.snap.nodes["b"] is b
    assert len(store.snap.edges) == 1


class _FakeResult:
    def __init__(self, rows=None) -> None:
        self._rows = rows or []
        self.data_called = False

    def __iter__(self):
        return iter(self._rows)

    def data(self):
        self.data_called = True
        return self._rows


class _FakeSession:
    """Records runs; execute_write invokes the tx function against itself."""

    def __init__(self, node_rows=None, edge_rows=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._node_rows = node_rows or []
        self._edge_rows = edge_rows or []
        self.managed_writes = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute_write(self, fn):
        self.managed_writes += 1
        return fn(self)

    def run(self, query, **kwargs):
        self.calls.append((query, kwargs))
        if "labels(n)" in query:
            return _FakeResult(self._node_rows)
        if "type(r)" in query:
            return _FakeResult(self._edge_rows)
        return _FakeResult([])


def test_neo4j_save_batches_rows(monkeypatch):
    session = _FakeSession()
    store = Neo4jStore()
    monkeypatch.setattr(store, "_session", lambda: session)

    snap = GraphSnapshot()
    for i in range(NEO4J_BATCH_SIZE + 10):
        snap.add_node(_node(f"n{i:06d}"))
    snap.add_edge(GraphEdge(from_id="n000000", to_id="n000001", kind="CALLS"))
    store.save(snap)

    node_calls = [(q, k) for q, k in session.calls if "UNWIND" in q and "MERGE (x:" in q]
    assert len(node_calls) == 2
    assert len(node_calls[0][1]["rows"]) == NEO4J_BATCH_SIZE
    assert node_calls[0][1]["rows"][0]["props"]["id"] == "n000000"
    edge_calls = [(q, k) for q, k in session.calls if "CREATE (a)-" in q]
    assert len(edge_calls) == 1
    assert "(a:Symbol {id: row.a})" in edge_calls[0][0]  # labelled match
    deletes = [q for q, _ in session.calls if "DETACH DELETE" in q]
    assert len(deletes) == 1
    assert "IN TRANSACTIONS" not in deletes[0]
    # Delete and every chunk share one managed transaction.
    assert session.managed_writes == 1


def test_neo4j_load_is_cached_and_streamed(monkeypatch):
    props = {"id": "n1", "name": "n1", "repo_id": "r", "line_start": 0, "line_end": 0}
    session = _FakeSession(node_rows=[{"labels": ["Symbol"], "props": props}])
    store = Neo4jStore()
    monkeypatch.setattr(store, "_session", lambda: session)

    first = store.load()
    assert set(first.nodes) == {"n1"}
    calls_after_first = len(session.calls)
    second = store.load()
    assert second is first
    assert len(session.calls) == calls_after_first  # no MATCH (n) on cache hit

    # Saving mirrors into the cache: the next load must not rehydrate.
    incoming = GraphSnapshot()
    incoming.add_node(_node("n2"))
    store.save(incoming)
    calls_after_save = len(session.calls)
    third = store.load()
    assert third is not first
    assert set(first.nodes) == {"n1"}
    assert set(third.nodes) == {"n2"}  # repo r replaced, like the DB write
    assert len(session.calls) == calls_after_save


def test_neo4j_load_is_single_flight(monkeypatch):
    import threading
    import time

    session_count = [0]

    def slow_session():
        session_count[0] += 1
        time.sleep(0.2)
        return _FakeSession()

    store = Neo4jStore()
    monkeypatch.setattr(store, "_session", slow_session)
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(store.load())) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert session_count[0] == 1
    assert all(r is results[0] for r in results)


def test_snapshot_round_trip_keeps_by_kind_working(fixtures):
    from axon.v2.indexer import index_repo

    snap, _ = index_repo(str(fixtures / "monorepo"), use_cache=False)
    loaded = GraphSnapshot.model_validate_json(snap.model_dump_json())
    ids = [n.id for n in loaded.by_kind(NodeKind.SYMBOL)]
    assert ids == sorted(ids) and ids
    loaded.add_node(_node("zz-new"))
    assert "zz-new" in {n.id for n in loaded.by_kind(NodeKind.SYMBOL)}


def test_memory_save_keeps_old_reader_snapshot():
    old = GraphSnapshot(nodes={"old": _node("old")})
    store = MemoryStore(old)
    store.save(GraphSnapshot(nodes={"new": _node("new")}))
    assert set(old.nodes) == {"old"}
    assert set(store.load().nodes) == {"new"}


def test_failed_neo4j_write_does_not_publish_cache(monkeypatch):
    old = GraphSnapshot(nodes={"old": _node("old")})
    store = Neo4jStore()
    store._cached = old
    session = _FakeSession()

    def fail(query, **kwargs):
        if "UNWIND" in query:
            raise RuntimeError("write failed")
        return _FakeResult()

    session.run = fail
    monkeypatch.setattr(store, "_session", lambda: session)
    with pytest.raises(RuntimeError, match="write failed"):
        store.save(GraphSnapshot(nodes={"new": _node("new")}))
    assert store.load() is old
    assert session.managed_writes == 1


def test_neo4j_edge_properties_and_detail_round_trip(monkeypatch):
    from axon.v2.models import Evidence

    edge = GraphEdge(
        from_id="a",
        to_id="b",
        kind="CALLS",
        evidence=Evidence(file="app.py", line=3, rule="binding", detail="factory"),
        props={"candidate_ids": "a,b", "line": "custom"},
    )
    session = _FakeSession()
    store = Neo4jStore()
    monkeypatch.setattr(store, "_session", lambda: session)
    store.save(GraphSnapshot(nodes={"a": _node("a"), "b": _node("b")}, edges=[edge]))
    properties = next(k["rows"][0]["props"] for q, k in session.calls if "CREATE (a)-" in q)
    assert properties["candidate_ids"] == "a,b"
    assert properties["detail"] == "factory"
    session._edge_rows = [{"a": "a", "b": "b", "kind": "CALLS", "props": properties}]
    assert store.load().edges == [edge]


def test_neo4j_delta_only_writes_changed_rows_and_publishes(monkeypatch):
    from axon.v2.graph_delta import GraphDelta, edge_key

    a, b = _node("a"), _node("b")
    edge = GraphEdge(from_id="a", to_id="b", kind="CALLS")
    old = GraphSnapshot(nodes={"a": a, "b": b}, edges=[edge])
    changed = a.model_copy(update={"name": "updated"})
    target = GraphSnapshot(nodes={"a": changed, "b": b})
    store = Neo4jStore()
    store._cached = old
    session = _FakeSession()
    monkeypatch.setattr(store, "_session", lambda: session)
    store.apply_delta(GraphDelta.between(old, target))
    node_rows = [k["rows"] for q, k in session.calls if "MERGE (x:" in q]
    assert len(node_rows) == 1 and [r["id"] for r in node_rows[0]] == ["a"]
    assert not any("repo_id: $repo_id" in q for q, _ in session.calls)
    assert any(edge_key(edge) in k.get("keys", []) for _, k in session.calls)
    assert store.load().model_dump() == target.model_dump()
    assert store.load() is not old
    assert old.nodes["a"].name == "a" and old.edges == [edge]
    assert session.managed_writes == 1


def test_neo4j_delta_failure_keeps_cached_snapshot(monkeypatch):
    from axon.v2.graph_delta import GraphDelta

    store = Neo4jStore()
    old = GraphSnapshot(nodes={"a": _node("a")})
    store._cached = old
    session = _FakeSession()
    original_run = session.run

    def run(query, **kwargs):
        if "MERGE (x:" in query:
            raise RuntimeError("write failed")
        return original_run(query, **kwargs)

    session.run = run
    monkeypatch.setattr(store, "_session", lambda: session)
    with pytest.raises(RuntimeError, match="write failed"):
        store.apply_delta(GraphDelta(upsert_nodes={"b": _node("b")}))
    assert store.load() is old
    assert session.managed_writes == 1


def test_neo4j_delta_requires_full_save_for_affected_legacy_edges(monkeypatch):
    from axon.v2.graph_delta import GraphDelta
    from axon.v2.store import DeltaNeedsFullSave

    store = Neo4jStore()
    old = GraphSnapshot(nodes={"a": _node("a")})
    store._cached = old
    session = _FakeSession()
    original_run = session.run

    def run(query, **kwargs):
        if "RETURN 1 AS legacy" in query:
            return _FakeResult([{"legacy": 1}])
        return original_run(query, **kwargs)

    session.run = run
    monkeypatch.setattr(store, "_session", lambda: session)
    with pytest.raises(DeltaNeedsFullSave, match="full repository"):
        store.apply_delta(GraphDelta(upsert_nodes={"a": _node("a")}))
    assert store.load() is old
    assert session.calls == []


def test_neo4j_save_persists_complete_edge_key(monkeypatch):
    from axon.v2.graph_delta import edge_key

    session = _FakeSession()
    store = Neo4jStore()
    monkeypatch.setattr(store, "_session", lambda: session)
    edge = GraphEdge(from_id="a", to_id="b", kind="CALLS")
    store.save(GraphSnapshot(nodes={"a": _node("a"), "b": _node("b")}, edges=[edge]))
    rows = next(k["rows"] for q, k in session.calls if "CREATE (a)-" in q)
    assert rows[0]["props"]["_axon_key"] == edge_key(edge)


def test_neo4j_replacement_collects_orphans_inside_write_transaction(monkeypatch):
    store = Neo4jStore()
    session = _FakeSession()
    monkeypatch.setattr(store, "_session", lambda: session)
    store.save(GraphSnapshot(nodes={"a": _node("a")}))
    assert session.managed_writes == 1
    assert "MERGE (x:" in session.calls[-2][0]
    query = session.calls[-1][0]
    assert "ExternalApi" in query and "NOT (n)--()" in query
