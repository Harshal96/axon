from __future__ import annotations

import pytest

from axon.v1.graph import GraphSnapshot
from axon.v1.models import GraphEdge, GraphNode, NodeKind
from axon.v1.store import NEO4J_BATCH_SIZE, MemoryStore, Neo4jStore

pytestmark = pytest.mark.tier1


@pytest.fixture(autouse=True)
def local_safety_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("AXON_STATE_DIR", str(tmp_path))


def _owned(snap):
    for repo in {n.repo_id for n in list(snap.nodes.values()) if n.repo_id}:
        snap.add_node(GraphNode(id=f"repo::{repo}", kind=NodeKind.REPO, repo_id=repo))
    return snap


def _node(nid: str, repo: str = "r") -> GraphNode:
    return GraphNode(id=nid, kind=NodeKind.SYMBOL, name=nid, repo_id=repo)


def test_memory_store_save_replaces_repo_without_copy():
    store = MemoryStore()
    first = GraphSnapshot()
    a = _node("a")
    first.add_node(a)
    first.add_node(GraphNode(id="repo::r", kind=NodeKind.REPO, repo_id="r"))
    store.save(_owned(first))

    second = GraphSnapshot()
    b = _node("b")
    second.add_node(b)
    second.add_node(GraphNode(id="repo::r", kind=NodeKind.REPO, repo_id="r"))
    second.add_edge(GraphEdge(from_id="b", to_id="b", kind="CALLS"))
    store.save(_owned(second))

    assert set(store.snap.nodes) == {"b", "repo::r"}
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
        if "AS written" in query:
            return _FakeResult([{"written": len(kwargs["rows"])}])
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
    store.save(_owned(snap))

    node_calls = [(q, k) for q, k in session.calls if "UNWIND" in q and "CREATE (x:Symbol" in q]
    assert len(node_calls) == 2
    assert len(node_calls[0][1]["rows"]) == NEO4J_BATCH_SIZE
    assert node_calls[0][1]["rows"][0]["props"]["id"] == "n000000"
    edge_calls = [(q, k) for q, k in session.calls if "CREATE (a)-" in q]
    assert len(edge_calls) == 1
    assert "(a:Symbol {_axon_storage_id: row.a})" in edge_calls[0][0]  # labelled match
    deletes = [q for q, _ in session.calls if "WITH n LIMIT $batch DELETE n" in q]
    assert len(deletes) >= len(NodeKind)
    assert all("LIMIT $batch" in query for query in deletes)
    # Staging batches, publication, and cleanup have separate transactions.
    assert session.managed_writes > len(node_calls) + len(edge_calls)


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
    incoming.add_node(GraphNode(id="repo::r", kind=NodeKind.REPO, repo_id="r"))
    store.save(_owned(incoming))
    calls_after_save = len(session.calls)
    third = store.load()
    assert third is not first
    assert set(first.nodes) == {"n1"}
    assert set(third.nodes) == {"n2", "repo::r"}  # repo r replaced, like the DB write
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
    from axon.v1.indexer import index_repo

    snap, _ = index_repo(str(fixtures / "monorepo"), use_cache=False)
    loaded = GraphSnapshot.model_validate_json(snap.model_dump_json())
    ids = [n.id for n in loaded.by_kind(NodeKind.SYMBOL)]
    assert ids == sorted(ids) and ids
    loaded.add_node(_node("zz-new"))
    assert "zz-new" in {n.id for n in loaded.by_kind(NodeKind.SYMBOL)}


def test_memory_save_keeps_old_reader_snapshot():
    old = GraphSnapshot(nodes={"old": _node("old")})
    store = MemoryStore(old)
    store.save(
        _owned(
            GraphSnapshot(
                nodes={
                    "new": _node("new"),
                    "repo::r": GraphNode(id="repo::r", kind=NodeKind.REPO, repo_id="r"),
                }
            )
        )
    )
    assert set(old.nodes) == {"old"}
    assert set(store.load().nodes) == {"new", "repo::r"}


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
        store.save(_owned(GraphSnapshot(nodes={"new": _node("new")})))
    assert store.load() is old
    assert session.managed_writes == 1


def test_neo4j_edge_properties_and_detail_round_trip(monkeypatch):
    from axon.v1.models import Evidence

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
    store.save(_owned(GraphSnapshot(nodes={"a": _node("a"), "b": _node("b")}, edges=[edge])))
    properties = next(k["rows"][0]["props"] for q, k in session.calls if "CREATE (a)-" in q)
    assert properties["candidate_ids"] == "a,b"
    assert properties["detail"] == "factory"
    session._edge_rows = [{"a": "a", "b": "b", "kind": "CALLS", "props": properties}]
    assert store.load().edges == [edge]


def test_neo4j_delta_stages_replacement_and_preserves_old_reader(monkeypatch):
    from axon.v1.graph_delta import GraphDelta

    a, b = _node("a"), _node("b")
    edge = GraphEdge(from_id="a", to_id="b", kind="CALLS")
    old = _owned(GraphSnapshot(nodes={"a": a, "b": b}, edges=[edge]))
    changed = a.model_copy(update={"name": "updated"})
    target = _owned(GraphSnapshot(nodes={"a": changed, "b": b}))
    store = Neo4jStore()
    store._cached = old
    session = _FakeSession()
    monkeypatch.setattr(store, "_session", lambda: session)
    store.apply_delta(GraphDelta.between(old, target))
    node_rows = [r for q, k in session.calls if "CREATE (x:Symbol" in q for r in k["rows"]]
    assert {r["id"] for r in node_rows} == {"a", "b"}
    assert store.load().nodes == target.nodes
    assert store.load().edges == []
    assert store.load() is not old
    assert old.nodes["a"].name == "a" and old.edges == [edge]
    assert session.managed_writes > 1


def test_neo4j_delta_failure_keeps_cached_snapshot(monkeypatch):
    from axon.v1.graph_delta import GraphDelta

    store = Neo4jStore()
    old = _owned(GraphSnapshot(nodes={"a": _node("a")}))
    store._cached = old
    session = _FakeSession()
    original_run = session.run

    def run(query, **kwargs):
        if "CREATE (x:" in query:
            raise RuntimeError("write failed")
        return original_run(query, **kwargs)

    session.run = run
    monkeypatch.setattr(store, "_session", lambda: session)
    with pytest.raises(RuntimeError, match="write failed"):
        store.apply_delta(GraphDelta(upsert_nodes={"b": _node("b")}))
    assert store.load() is old
    assert session.managed_writes == 1


def test_neo4j_delta_migrates_legacy_without_in_place_mutation(monkeypatch):
    from axon.v1.graph_delta import GraphDelta

    store = Neo4jStore()
    old = _owned(GraphSnapshot(nodes={"a": _node("a")}))
    store._cached = old
    session = _FakeSession()
    monkeypatch.setattr(store, "_session", lambda: session)
    store.apply_delta(GraphDelta(upsert_nodes={"a": _node("a")}))
    assert store.load() is not old
    assert store.load().nodes == old.nodes
    assert not any("SET x =" in q and "MERGE (x:" in q for q, _ in session.calls)


def test_neo4j_save_persists_complete_edge_key(monkeypatch):
    from axon.v1.graph_delta import edge_key

    session = _FakeSession()
    store = Neo4jStore()
    monkeypatch.setattr(store, "_session", lambda: session)
    edge = GraphEdge(from_id="a", to_id="b", kind="CALLS")
    store.save(_owned(GraphSnapshot(nodes={"a": _node("a"), "b": _node("b")}, edges=[edge])))
    rows = next(k["rows"] for q, k in session.calls if "CREATE (a)-" in q)
    assert rows[0]["props"]["_axon_key"] == edge_key(edge)


def test_neo4j_replacement_cleans_obsolete_rows_after_publication(monkeypatch):
    store = Neo4jStore()
    session = _FakeSession()
    monkeypatch.setattr(store, "_session", lambda: session)
    store.save(_owned(GraphSnapshot(nodes={"a": _node("a")})))
    publish = next(i for i, (q, _) in enumerate(session.calls) if "SET p.generation" in q)
    deletes = [
        (i, q) for i, (q, _) in enumerate(session.calls) if "WITH n LIMIT $batch DELETE n" in q
    ]
    assert deletes and all(i > publish and "LIMIT $batch" in q for i, q in deletes)
    assert all("MATCH (n:" in q for _, q in deletes)


def test_full_save_stages_bounded_transactions_before_publish(monkeypatch):
    session = _FakeSession()
    store = Neo4jStore()
    store._cached = GraphSnapshot()
    monkeypatch.setattr(store, "_session", lambda: session)
    snap = GraphSnapshot(nodes={f"n{i}": _node(f"n{i}") for i in range(NEO4J_BATCH_SIZE + 1)})
    store.save(_owned(snap))
    writes = [(q, k) for q, k in session.calls if "CREATE (x:" in q]
    assert len(writes) >= 2
    assert all(len(k["rows"]) <= NEO4J_BATCH_SIZE for _, k in writes)
    publish = next(i for i, (q, _) in enumerate(session.calls) if "SET p.generation" in q)
    assert all(i < publish for i, (q, _) in enumerate(session.calls) if "CREATE (x:" in q)
    assert session.managed_writes >= 3


def test_reindex_unchanged_graph_uses_safe_replacement_staging(monkeypatch):
    session = _FakeSession()
    snap = GraphSnapshot(nodes={"a": _node("a")})
    store = Neo4jStore()
    store._cached = snap
    monkeypatch.setattr(store, "_session", lambda: session)
    store.save(_owned(snap))
    assert any("CREATE (x:" in q for q, _ in session.calls)
    assert any("SET p.generation" in q for q, _ in session.calls)


def test_generation_query_scope_covers_all_nodes_and_preserves_strings():
    from axon.v1.store import scope_active_generations

    query = "MATCH (a:Symbol)-[:CALLS]->(b) WHERE a.name = '(fake)' RETURN a, b"
    scoped = scope_active_generations(query)
    assert "a._axon_generation" in scoped
    assert "b._axon_generation" in scoped
    assert "fake._axon_generation" not in scoped
    assert "'(fake)'" in scoped


def test_transaction_memory_error_names_batch_and_settings(monkeypatch):
    session = _FakeSession()
    store = Neo4jStore()
    store._cached = GraphSnapshot()
    monkeypatch.setattr(store, "_session", lambda: session)
    original = session.run

    def fail(query, **kwargs):
        if "UNWIND" in query:
            raise RuntimeError("MemoryPoolOutOfMemoryError: threshold reached")
        return original(query, **kwargs)

    session.run = fail
    with pytest.raises(RuntimeError, match="Symbol.*AXON_NEO4J_TX_MAX"):
        store.save(_owned(GraphSnapshot(nodes={"a": _node("a")})))
    assert not any("SET p.generation" in q for q, _ in session.calls)


def test_inline_or_predicate_is_grouped_before_generation_filter():
    from axon.v1.store import scope_active_generations

    query = "MATCH (n WHERE n.name = 'x' OR n.name = 'y') RETURN n"
    scoped = scope_active_generations(query)
    assert "WHERE ( n.name = 'x' OR n.name = 'y') AND (" in scoped


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n:Repo) RETURN [(n)-->(m) | m] AS targets",
        "MATCH (n:Repo) WITH [(n)-->(m) | m] AS targets RETURN targets",
        "MATCH (n:Repo) RETURN EXISTS { (n)-->(:Symbol) } AS found",
        "MATCH (n:Repo) RETURN exists((n)-->(:Symbol)) AS found",
        "MATCH p=(a)-[*1..5]->(b) RETURN p",
        "MATCH (n) RETURN COUNT { MATCH (m) RETURN m }",
    ],
)
def test_generation_scope_rejects_unfilterable_graph_patterns(query):
    from axon.v1.store import scope_active_generations

    with pytest.raises(ValueError, match="generation-safe"):
        scope_active_generations(query)


def test_shared_external_storage_survives_other_repository_generation(monkeypatch):
    from axon.v1.graph_delta import GraphDelta

    session = _FakeSession()
    store = Neo4jStore()
    store._cached = GraphSnapshot()
    monkeypatch.setattr(store, "_session", lambda: session)
    external = GraphNode(id="external::GET:/unknown", kind=NodeKind.EXTERNAL_API)

    def snapshot(repo):
        node = _node(repo, repo)
        return GraphSnapshot(
            nodes={repo: node, external.id: external},
            edges=[GraphEdge(from_id=repo, to_id=external.id, kind="CALLS_API")],
        )

    store.save(_owned(snapshot("a")))
    store.save(_owned(snapshot("b")))
    edge = GraphEdge(
        from_id="a", to_id=external.id, kind="CALLS_API", props={"review": "second-call"}
    )
    store.apply_delta(GraphDelta(add_edges=[edge]))
    row = next(k["rows"][0] for q, k in reversed(session.calls) if "CREATE (a)-" in q)
    assert row["b"] == f"{store.load().publication_id}:external:{external.id}"
    store.save(_owned(snapshot("b")))
    assert {e.from_id for e in store.load().edges if e.kind.value == "CALLS_API"} == {"a", "b"}
    external_rows = [
        row["props"] for q, k in session.calls if "CREATE (x:ExternalApi" in q for row in k["rows"]
    ]
    assert external_rows
    assert all(r["_axon_link_generation"] for r in external_rows)


def test_progress_callback_is_thread_local():
    import threading

    store = Neo4jStore()

    def callback(done, total):
        pass

    store.publish_progress = callback
    seen = []
    thread = threading.Thread(target=lambda: seen.append(store.publish_progress))
    thread.start()
    thread.join()
    assert seen == [None]
    assert store.publish_progress is callback


def test_generation_scope_rejects_grouped_quantified_path():
    from axon.v1.store import scope_active_generations

    with pytest.raises(ValueError, match="grouped"):
        scope_active_generations("MATCH p=((a)-->(b)){1,3} RETURN p")


def test_new_storage_avoids_full_json_property_copy_and_keeps_collisions(monkeypatch):
    node = _node("a").model_copy(update={"props": {"name": "custom", "qualified": "app.f"}})
    row = Neo4jStore._node_row(node, "generation:a", "r", "generation")
    assert "_axon_props" not in row["props"]
    assert row["props"]["qualified"] == "app.f"
    session = _FakeSession(node_rows=[{"labels": ["Symbol"], "props": row["props"]}])
    store = Neo4jStore()
    monkeypatch.setattr(store, "_session", lambda: session)
    assert store.load().nodes["a"].props == node.props


def test_large_delta_can_remove_entire_repository(monkeypatch):
    from axon.v1.graph_delta import GraphDelta

    session = _FakeSession()
    store = Neo4jStore()
    store._cached = _owned(GraphSnapshot(nodes={f"n{i}": _node(f"n{i}") for i in range(3)}))
    monkeypatch.setattr(store, "_session", lambda: session)
    monkeypatch.setattr("axon.v1.store.NEO4J_BATCH_SIZE", 2)
    store.apply_delta(GraphDelta(remove_node_ids=set(store._cached.nodes)))
    assert not store.load().nodes
    publication = next(k for q, k in session.calls if "SET p.generation" in q)
    assert publication["rows"][0]["markers"][0]["repo_id"] == "r"
    assert any("WITH n LIMIT $batch DELETE n" in q for q, _ in session.calls)


def test_generation_cleanup_removes_only_orphan_shared_placeholders_in_batches(monkeypatch):
    session = _FakeSession()
    original_run = session.run
    orphan_batches = []

    def run(query, **kwargs):
        result = original_run(query, **kwargs)
        if "MATCH (n:ExternalApi)" in query and "NOT (n)--()" in query:
            orphan_batches.append(kwargs["batch"])
            return _FakeResult([{"removed": 2 if len(orphan_batches) == 1 else 1}])
        return result

    session.run = run
    store = Neo4jStore()
    store._cached = GraphSnapshot()
    monkeypatch.setattr(store, "_session", lambda: session)
    monkeypatch.setattr("axon.v1.store.NEO4J_BATCH_SIZE", 2)
    store.save(_owned(GraphSnapshot(nodes={"a": _node("a")})))
    assert orphan_batches == [2, 2]
    orphan_positions = [
        i
        for i, (q, _) in enumerate(session.calls)
        if "MATCH (n:ExternalApi)" in q and "NOT (n)--()" in q
    ]
    old_edge_positions = [i for i, (q, _) in enumerate(session.calls) if "DELETE r" in q]
    assert min(orphan_positions) > max(old_edge_positions)
    query = session.calls[orphan_positions[0]][0]
    assert "MATCH (n:ExternalApi)" in query
    assert "n._axon_link_generation IN [p.generation,p.prev_generation]" in query
    assert "LIMIT $batch DELETE n" in query
    assert "DETACH" not in query


def test_generation_scope_guards_named_and_anonymous_relationships_per_clause():
    from axon.v1.store import scope_active_generations

    scoped = scope_active_generations(
        "MATCH (a)-[r:CALLS_API]->(b) WHERE a.name = 'x' OR a.name = 'y' "
        "OPTIONAL MATCH (b)-->(c) WHERE c.name = 'z' RETURN a, b, c"
    )
    first, optional = scoped.split("OPTIONAL MATCH")
    assert "r._axon_link_generation" in first
    assert "WHERE ( a.name = 'x' OR a.name = 'y' " in first
    assert "-[_axon_rel_0]->" in optional
    assert "_axon_rel_0._axon_link_generation" in optional
    assert "WHERE ( c.name = 'z' " in optional
    assert "_AxonLinkPublication" in scoped
    assert "STARTS WITH '_Axon'" in scoped


@pytest.mark.parametrize(
    "pattern", ["(a)-[:CALLS_API]->(b)", "(a)<--(b)", "(a)--(b)", "(a)-[]->(b)"]
)
def test_generation_scope_names_every_bounded_relationship(pattern):
    from axon.v1.store import scope_active_generations

    scoped = scope_active_generations(f"MATCH {pattern} RETURN a, b")
    assert "_axon_rel_0._axon_link_generation" in scoped
    assert "[_axon_rel_0" in scoped


def test_generation_scope_avoids_alias_collisions():
    from axon.v1.store import scope_active_generations

    scoped = scope_active_generations("MATCH (_axon_rel_0)-->() RETURN _axon_rel_0")
    assert "[_axon_rel_1]" in scoped


def test_hydration_hides_all_private_metadata_props(monkeypatch):
    node = _node("a")
    row = Neo4jStore._node_row(node, "g:a", "r", "g")
    row["props"].update(
        {"_axon_link_generation": "g", "_axon_future": "private", "custom": "public"}
    )
    session = _FakeSession(
        node_rows=[{"labels": ["Symbol"], "props": row["props"]}],
        edge_rows=[
            {
                "a": "a",
                "b": "a",
                "kind": "CALLS_API",
                "props": {
                    "_axon_link_generation": "g",
                    "_axon_future": "private",
                    "custom": "public",
                },
            }
        ],
    )
    store = Neo4jStore()
    monkeypatch.setattr(store, "_session", lambda: session)
    snap = store.load()
    assert snap.nodes["a"].props == {"custom": "public"}
    assert snap.edges[0].props == {"custom": "public"}
    node_query = next(q for q, _ in session.calls if "labels(n)" in q)
    edge_query = next(q for q, _ in session.calls if "type(r)" in q)
    assert "STARTS WITH '_Axon'" in node_query
    assert "r._axon_link_generation" in edge_query


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (a)-[r:CALLS_API {values: [1,2]}]->(b) RETURN a, b",
        "MATCH (a)-[r:CALLS_API WHERE r.name = 'x']->(b) RETURN a, b",
        "MATCH p=shortestPath((a)-[:CALLS_API]->(b)) RETURN p",
    ],
)
def test_generation_scope_fails_closed_on_unparsed_relationship_forms(query):
    from axon.v1.store import scope_active_generations

    with pytest.raises(ValueError, match="generation-safe"):
        scope_active_generations(query)


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) RETURN [(n) - [r:CALLS_API] -> (m) | m] AS targets",
        "MATCH (n) WITH [(n) <- [r:CALLS_API] - (m) | m] AS targets RETURN targets",
        "MATCH (n WHERE size([(n)-[:CALLS_API]->(m) | m]) > 0) RETURN n",
    ],
)
def test_generation_scope_rejects_nested_and_spaced_pattern_comprehensions(query):
    from axon.v1.store import scope_active_generations

    with pytest.raises(ValueError, match="generation-safe"):
        scope_active_generations(query)


def test_model_query_results_recursively_hide_private_maps_and_graph_entities(monkeypatch):
    from neo4j.graph import Graph, Node, Path, Relationship

    graph = Graph()
    node = Node(graph, "n", 1, ["Symbol"], {"id": "a", "_axon_generation": "secret"})
    target = Node(graph, "m", 2, ["Symbol"], {"id": "b", "_axon_generation": "secret"})
    edge = Relationship(graph, "r", 3, {"name": "public", "_axon_link_generation": "secret"})
    edge._start_node = node
    edge._end_node = target
    path = Path(node, edge)
    raw = {
        "node_props": dict(node),
        "edge_props": dict(edge),
        "node": node,
        "edge": edge,
        "nested": {"list": [{"public": 7, "_axon_future": "secret"}, node, edge]},
        "path": path,
        "scalar": 3,
    }
    session = _FakeSession()
    session.run = lambda query: _FakeResult([raw])
    store = Neo4jStore()
    monkeypatch.setattr(store, "_session", lambda: session)
    rows = store.run_cypher(
        "MATCH (n)-[r:CALLS_API]->(m) RETURN properties(n), properties(r), n, r, {nested:[n,r]}"
    )
    assert "secret" not in str(rows)
    assert "_axon_" not in str(rows)
    assert rows[0]["node"] == "{'id': 'a'}"
    assert rows[0]["edge"] == "{'name': 'public'}"
    assert "'public': 7" in rows[0]["nested"]
    assert "'id': 'b'" in rows[0]["path"]
    assert rows[0]["scalar"] == 3


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) RETURN n._axon_generation AS renamed",
        "MATCH (n {_axon_generation:'secret'}) RETURN n.id",
        "MATCH (n) RETURN n['_axon_generation'] AS renamed",
        'MATCH (n) RETURN n["_axon_generation"] AS renamed',
        "MATCH (n) RETURN properties(n)['_axon_generation'] AS renamed",
        "MATCH (n) WITH properties(n) AS p RETURN p._axon_generation AS renamed",
        "WITH '_axon_generation' AS key MATCH (n) RETURN n[key] AS renamed",
        "MATCH (n) RETURN n['_' + 'axon_generation'] AS renamed",
        r"MATCH (n) RETURN n['\u005faxon_generation'] AS renamed",
        "MATCH (n) RETURN keys(n)",
        "MATCH (n) WITH properties(n) AS p RETURN keys(p)",
        "MATCH (n) RETURN apoc.convert.toJson(properties(n)) AS renamed",
        "MATCH (n) RETURN κ(properties(n)) AS renamed",
    ],
)
def test_model_queries_fail_closed_on_private_access_and_property_laundering(query):
    from axon.v1.store import scope_active_generations

    with pytest.raises(ValueError, match="generation-safe"):
        scope_active_generations(query)


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) WHERE n.name IN ['public'] RETURN n.id, n['name'], properties(n)",
        "MATCH (n) RETURN n {.*, public:n.name} AS props",
        "MATCH (n)-[r]->(m) RETURN type(r), labels(n), count(r), collect(properties(r))",
        "MATCH p=(n)-[r]->(m) RETURN nodes(p), relationships(p), length(p)",
        "MATCH (n) RETURN coalesce(n.name, 'unknown'), toLower(n.name), collect(n)[0]",
        "MATCH (n) WHERE n.name = '_axon_generation' RETURN n.name",
        "MATCH (n) WHERE n.name = '雪' RETURN n.name",
    ],
)
def test_model_query_known_public_forms_remain_supported(query):
    from axon.v1.store import scope_active_generations

    assert scope_active_generations(query)


@pytest.mark.parametrize(
    "query",
    [
        'MATCH (n) RETURN n {.*}["_axon_generation"] AS leak',
        "MATCH (n) RETURN n {.*}['_axon_generation'] AS leak",
        "WITH '_axon_generation' AS key MATCH (n) RETURN n {.*}[key] AS leak",
        "MATCH (n) RETURN n {.*}['_' + 'axon_generation'] AS leak",
        "MATCH (n) RETURN n {.*} /* gap */ ['_axon_generation'] AS leak",
        "MATCH (n) RETURN n {nested:n {.*}}['nested']['_axon_generation'] AS leak",
        "WITH '_axon_generation' AS key MATCH (n) RETURN n {nested:n {.*}[key]} AS leak",
    ],
)
def test_model_queries_reject_private_or_computed_map_projection_subscripts(query):
    from axon.v1.store import scope_active_generations

    with pytest.raises(ValueError, match="generation-safe"):
        scope_active_generations(query)


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) RETURN n {.*}['name'] AS name",
        'MATCH (n) RETURN n {.name}["name"] AS name',
        "MATCH (n) RETURN n {nested:n {.*}}['nested']['name'] AS name",
        "MATCH (n) RETURN n {nested:n {.*}['name']}['nested'] AS name",
    ],
)
def test_model_queries_accept_static_public_map_projection_subscripts(query):
    from axon.v1.store import scope_active_generations

    assert scope_active_generations(query)
