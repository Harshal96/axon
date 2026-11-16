"""Infrastructure-free persistent publication protocol contracts."""

import pytest

from axon.v1.graph_delta import GraphDelta
from axon.v1.models import EdgeKind, endpoint_node_id
from axon.v1.store import Neo4jStore
from tests.v1.test_multirepo_linking import caller, outbound, repository
from tests.v1.test_store import _FakeSession


@pytest.fixture
def store(monkeypatch, tmp_path):
    session = _FakeSession()
    result = Neo4jStore()
    monkeypatch.setattr(result, "_session", lambda: session)
    monkeypatch.setenv("AXON_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("axon.v1.store.NEO4J_BATCH_SIZE", 2)
    return result, session


@pytest.mark.parametrize("order", [("A", "B"), ("B", "A")])
def test_persistent_publication_replays_both_index_orders(store, order):
    writer, session = store
    snapshots = {"A": caller(), "B": repository("B", "ledger")}
    for repo in order:
        writer.publish_repository(snapshots[repo], repo_id=repo)
    current = writer.load()
    assert outbound(current)[10].to_id == endpoint_node_id("B", "ledger", "GET", "/entries")
    assert set(current.link_inputs) == {"A", "B"}
    assert current.publication_id
    assert current.publication_stats.replayed_repositories == 2
    call_rows = [r for q, k in session.calls if "CREATE (a)-[r:CALLS_API]" in q for r in k["rows"]]
    assert call_rows[-1]["props"]["_axon_link_generation"] == current.publication_id
    assert all(len(k["rows"]) <= 2 for _, k in session.calls if "rows" in k)


def test_persistent_validation_and_staging_failures_preserve_old_reader(store, monkeypatch):
    writer, _ = store
    writer.publish_repository(caller(), repo_id="A")
    old = writer.load()

    def reject(candidate):
        assert "B" in candidate.link_inputs
        raise ValueError("rejected")

    with pytest.raises(ValueError, match="rejected"):
        writer.publish_repository(repository("B", "ledger"), repo_id="B", validate=reject)
    assert writer.load() is old
    original = writer._run_batch

    def fail(session, query, **kwargs):
        if kwargs["label"] == "publication":
            raise RuntimeError("staging interrupted")
        original(session, query, **kwargs)

    monkeypatch.setattr(writer, "_run_batch", fail)
    with pytest.raises(RuntimeError, match="staging interrupted"):
        writer.publish_repository(repository("B", "ledger"), repo_id="B")
    assert writer.load() is old


def test_persistent_stale_adapters_rebase_and_removal_replays(store):
    writer, _ = store
    from axon.v1.graph import GraphSnapshot

    empty = GraphSnapshot()
    a, b = GraphDelta.between(empty, caller()), GraphDelta.between(empty, repository("B", "ledger"))
    writer.apply_delta(a)
    old = writer.load()
    source = old.link_inputs["A"].source_generation
    writer.apply_delta(b)
    assert writer.load().link_inputs["A"].source_generation == source
    assert outbound(writer.load())[10].to_id == endpoint_node_id("B", "ledger", "GET", "/entries")
    writer.remove_repository("B")
    assert "B" not in writer.load().link_inputs
    assert outbound(writer.load())[10].to_id != endpoint_node_id("B", "ledger", "GET", "/entries")
    assert all(e.kind != EdgeKind.CALLS_API or e.from_id in old.nodes for e in old.edges)


def test_persistent_concurrent_publications_use_latest_inputs(store):
    import threading

    writer, _ = store
    barrier = threading.Barrier(2)
    failures = []

    def publish(snapshot, repo):
        try:
            barrier.wait()
            writer.publish_repository(snapshot, repo_id=repo)
        except Exception as exc:
            failures.append(exc)

    threads = [
        threading.Thread(target=publish, args=(caller(), "A")),
        threading.Thread(target=publish, args=(repository("B", "ledger"), "B")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures
    assert set(writer.load().link_inputs) == {"A", "B"}
    assert outbound(writer.load())[10].to_id == endpoint_node_id("B", "ledger", "GET", "/entries")


def test_staging_row_count_mismatch_aborts_publication(store, monkeypatch):
    from tests.v1.test_store import _FakeResult

    writer, session = store
    writer.save(caller())
    old = writer.load()
    original = session.run

    def truncated(query, **kwargs):
        if "AS written" in query and "CREATE (a)-" in query:
            return _FakeResult([{"written": 0}])
        return original(query, **kwargs)

    monkeypatch.setattr(session, "run", truncated)
    with pytest.raises(ValueError, match="row count mismatch"):
        writer.save(repository("B", "ledger"))
    assert writer.load() is old


def test_legacy_safety_activation_is_required_unique_and_keeps_schema(store, monkeypatch, tmp_path):
    import json

    from axon.v1.graph import GraphSnapshot
    from axon.v1.models import GraphNode, NodeKind

    writer, _ = store
    legacy = GraphSnapshot(
        schema_version=1,
        nodes={"repo::A": GraphNode(id="repo::A", kind=NodeKind.REPO, repo_id="A")},
    )
    monkeypatch.setenv("AXON_NO_SAFETY_SNAPSHOT", "1")
    writer._write_safety_snapshot(legacy, legacy)
    first = writer.last_safety_snapshot
    writer._write_safety_snapshot(legacy, legacy)
    assert first != writer.last_safety_snapshot
    paths = list((tmp_path / "safety").glob("*.json"))
    assert len(paths) == 2
    assert all(json.loads(path.read_text())["schema_version"] == 1 for path in paths)


def test_missing_published_inputs_and_reports_fail_closed(store, monkeypatch):
    from tests.v1.test_store import _FakeResult

    writer, session = store
    original = session.run

    def inputs(query, **kwargs):
        if query.startswith("MATCH (p:_AxonPublication)"):
            return _FakeResult(
                [
                    {
                        "repo_id": "A",
                        "generation": "source",
                        "input_generation": "inputs",
                        "schema_version": 2,
                    }
                ]
            )
        return original(query, **kwargs)

    monkeypatch.setattr(session, "run", inputs)
    with pytest.raises(ValueError, match="input metadata is missing"):
        writer.load()

    def reports(query, **kwargs):
        if query.startswith("MATCH (p:_AxonLinkPublication"):
            return _FakeResult(
                [{"props": {"generation": "global", "schema_version": 2, "report_repos": ["A"]}}]
            )
        return original(query, **kwargs)

    monkeypatch.setattr(session, "run", reports)
    with pytest.raises(ValueError, match="report metadata is missing"):
        writer.load()


def test_new_cypher_reader_checks_stored_schema_before_running_query(store, monkeypatch):
    from tests.v1.test_store import _FakeResult

    writer, session = store
    original = session.run

    def newer(query, **kwargs):
        if "p.schema_version AS schema_version" in query:
            return _FakeResult([{"schema_version": 99}])
        return original(query, **kwargs)

    monkeypatch.setattr(session, "run", newer)
    with pytest.raises(ValueError, match="Upgrade Axon"):
        writer.run_cypher("MATCH (n) RETURN n.id")


def test_cold_hydration_retries_a_crossing_publication(store, monkeypatch):
    from tests.v1.test_store import _FakeResult

    writer, session = store
    current = ["first"]
    original = session.run

    def crossing(query, **kwargs):
        if "RETURN p.generation AS generation" in query:
            return _FakeResult([{"generation": current[0]}])
        if query.startswith("MATCH (p:_AxonLinkPublication"):
            return _FakeResult([{"props": {"generation": current[0], "schema_version": 2}}])
        if "labels(n)" in query:
            value = current[0]
            current[0] = "second"
            return _FakeResult([{"labels": ["Symbol"], "props": {"id": "a", "name": value}}])
        return original(query, **kwargs)

    monkeypatch.setattr(session, "run", crossing)
    snap = writer.load()
    assert snap.publication_id == "second" and snap.nodes["a"].name == "second"


def test_pinned_external_metadata_never_resurrects_a_missing_api():
    from axon.v1.endpoint_catalog import EndpointCatalog
    from axon.v1.graph import GraphSnapshot
    from axon.v1.link_inputs import PinnedCallInput, RepoLinkInputs
    from axon.v1.models import GraphEdge, GraphNode, NodeKind
    from axon.v1.repository_links import resolve_repo_links
    from axon.v1.servicemap import ServiceMap

    source = GraphSnapshot(
        nodes={"caller": GraphNode(id="caller", kind=NodeKind.SYMBOL, repo_id="A")}
    )
    pin = PinnedCallInput(
        GraphEdge(from_id="caller", to_id="missing", kind="CALLS_API"),
        "missing",
        external_target=GraphNode(id="missing", kind=NodeKind.API_ENDPOINT, repo_id="B"),
    )
    result = resolve_repo_links(
        source,
        RepoLinkInputs("A", ServiceMap(repo="A"), pinned=[pin]),
        EndpointCatalog.from_snapshot(source),
    )
    assert "missing" not in result.placeholders
    assert all(node.kind == NodeKind.EXTERNAL_API for node in result.placeholders.values())
    assert result.edges[0].to_id != "missing"


def test_aggregate_reports_are_chunked_before_the_final_marker_switch(store):
    writer, session = store
    snapshot = caller()
    snapshot.diagnostics.append("structural diagnostic: " + "x" * 30000)
    writer.save(snapshot)
    rows = [
        row
        for query, params in session.calls
        if "CREATE (x:_AxonSnapshotReport)" in query
        for row in params["rows"]
    ]
    assert len(rows) > 1 and all(len(row["payload"]) <= 8192 for row in rows)
    marker = next(
        params["rows"][0] for query, params in session.calls if "SET p.generation" in query
    )
    assert "unresolved" not in marker and "diagnostics" not in marker
    assert all(len(params["rows"]) <= 2 for _, params in session.calls if "rows" in params)
