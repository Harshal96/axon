"""Publication boundary contracts for shared-catalog replay."""

import threading

import pytest

from axon.v1.models import endpoint_node_id
from axon.v1.store import MemoryStore
from tests.v1.test_multirepo_linking import caller, outbound, repository


def test_publication_replays_retained_callers_and_preserves_old_readers():
    store = MemoryStore()
    store.publish_repository(caller(), repo_id="A")
    old = store.load()
    old_edges = [edge.model_dump() for edge in old.edges]
    source_generation = old.link_inputs["A"].source_generation
    store.publish_repository(repository("B", "ledger", host=True), repo_id="B")
    current = store.load()
    assert outbound(current)[10].to_id == endpoint_node_id("B", "ledger", "GET", "/entries")
    assert current.link_inputs["A"].source_generation == source_generation
    assert source_generation
    assert [edge.model_dump() for edge in old.edges] == old_edges


def test_validation_failure_keeps_publication_unchanged():
    store = MemoryStore()
    store.publish_repository(caller(), repo_id="A")
    old = store.load()

    def reject(candidate):
        assert "B" in candidate.link_inputs
        raise ValueError("rejected")

    with pytest.raises(ValueError, match="rejected"):
        store.publish_repository(repository("B", "ledger"), repo_id="B", validate=reject)
    assert store.load() is old


def test_concurrent_distinct_publications_keep_both_repositories():
    store = MemoryStore()
    barrier = threading.Barrier(2)

    def publish(snapshot, repo_id):
        barrier.wait()
        store.publish_repository(snapshot, repo_id=repo_id)

    threads = [
        threading.Thread(target=publish, args=(caller(), "A")),
        threading.Thread(target=publish, args=(repository("B", "ledger"), "B")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert set(store.load().link_inputs) == {"A", "B"}
    assert outbound(store.load())[10].to_id == endpoint_node_id("B", "ledger", "GET", "/entries")


def test_remove_repository_replays_retained_callers():
    store = MemoryStore()
    store.publish_repository(caller(), repo_id="A")
    store.publish_repository(repository("B", "ledger"), repo_id="B")
    current = store.remove_repository("B")
    assert "B" not in current.link_inputs
    assert outbound(current)[10].to_id != endpoint_node_id("B", "ledger", "GET", "/entries")


def test_save_import_replays_retained_callers():
    store = MemoryStore()
    store.save(caller())
    before = store.load()
    store.save(repository("B", "ledger"))
    assert outbound(store.load())[10].to_id == endpoint_node_id("B", "ledger", "GET", "/entries")
    assert (
        before.link_inputs["A"].source_generation == store.load().link_inputs["A"].source_generation
    )


def test_delta_target_update_replays_retained_callers():
    from axon.v1.graph_delta import GraphDelta, replace_repositories

    store = MemoryStore()
    store.publish_repository(caller(), repo_id="A")
    store.publish_repository(repository("B", "ledger"), repo_id="B")
    old = store.load()
    replacement = repository("B", "ledger", endpoint=False)
    target = replace_repositories(old, replacement)
    store.apply_delta(GraphDelta.between(old, target))
    assert outbound(store.load())[10].to_id != endpoint_node_id("B", "ledger", "GET", "/entries")
    assert store.load().link_inputs["A"].source_generation == old.link_inputs["A"].source_generation
    assert store.load().link_inputs["B"].source_generation != old.link_inputs["B"].source_generation


def test_save_rejects_unowned_import():
    from axon.v1.graph import GraphSnapshot
    from axon.v1.models import GraphNode, NodeKind

    store = MemoryStore()
    with pytest.raises(ValueError, match="repository"):
        store.save(GraphSnapshot(nodes={"orphan": GraphNode(id="orphan", kind=NodeKind.SYMBOL)}))


def test_delta_cannot_inject_unprepared_calls():
    from axon.v1.graph_delta import GraphDelta
    from axon.v1.models import EdgeKind, GraphEdge

    store = MemoryStore()
    store.save(caller())
    store.save(repository("B", "ledger"))
    current = store.load()
    existing = outbound(current)[10]
    invented = GraphEdge(
        from_id=existing.from_id,
        to_id=existing.to_id,
        kind=EdgeKind.CALLS_API,
        evidence={"line": 999},
    )
    store.apply_delta(GraphDelta(add_edges=[invented]))
    assert 999 not in outbound(store.load())


def test_delta_removal_replays_retained_callers():
    from axon.v1.graph_delta import GraphDelta

    store = MemoryStore()
    store.save(caller())
    store.save(repository("B", "ledger"))
    before = store.load()
    ids = {node.id for node in before.nodes.values() if node.repo_id == "B"}
    store.apply_delta(GraphDelta(remove_node_ids=ids))
    assert "B" not in store.load().link_inputs
    assert outbound(store.load())[10].to_id != endpoint_node_id("B", "ledger", "GET", "/entries")
    assert (
        store.load().link_inputs["A"].source_generation == before.link_inputs["A"].source_generation
    )


def test_every_success_gets_a_new_publication_id_but_validation_failure_does_not():
    store = MemoryStore()
    first = store.publish_repository(caller(), repo_id="A")
    assert first.publication_id
    second = store.publish_repository(repository("B", "ledger"), repo_id="B")
    assert second.publication_id and second.publication_id != first.publication_id
    with pytest.raises(ValueError):
        store.publish_repository(
            caller(), repo_id="A", validate=lambda _: (_ for _ in ()).throw(ValueError())
        )
    assert store.load() is second
    assert store.load().publication_id == second.publication_id
    third = store.remove_repository("B")
    assert third.publication_id != second.publication_id


def test_link_only_delta_preserves_source_generation():
    from axon.v1.graph_delta import GraphDelta
    from axon.v1.models import EdgeKind, GraphEdge

    store = MemoryStore()
    first = store.publish_repository(caller(), repo_id="A")
    edge = next(edge for edge in first.edges if edge.kind == EdgeKind.CALLS_API)
    store.apply_delta(
        GraphDelta(
            add_edges=[GraphEdge(from_id=edge.from_id, to_id=edge.to_id, kind=EdgeKind.CALLS_API)]
        )
    )
    assert (
        store.load().link_inputs["A"].source_generation == first.link_inputs["A"].source_generation
    )


@pytest.mark.parametrize("order", [("A", "B"), ("B", "A")])
def test_import_order_converges(order):
    store = MemoryStore()
    snapshots = {"A": caller(), "B": repository("B", "ledger")}
    for repo in order:
        store.save(snapshots[repo])
    assert outbound(store.load())[10].to_id == endpoint_node_id("B", "ledger", "GET", "/entries")


def test_legacy_import_pins_observed_edges_without_inventing_inputs():
    from copy import deepcopy

    source = MemoryStore()
    source.save(caller())
    legacy = deepcopy(source.load())
    original = {line: edge.to_id for line, edge in outbound(legacy).items()}
    legacy.link_inputs.clear()
    legacy.link_reports.clear()
    imported = MemoryStore()
    imported.save(legacy)
    assert {line: edge.to_id for line, edge in outbound(imported.load()).items()} == original
    assert not imported.load().link_inputs["A"].prepared
    assert any("link-inputs-missing" in d for d in imported.load().diagnostics)


def test_replacement_cannot_claim_a_retained_repository_node_id():
    store = MemoryStore()
    store.save(caller())
    old = store.load()
    replacement = repository("B", "ledger")
    foreign = next(
        node for node in old.nodes.values() if node.repo_id == "A" and node.kind.value == "Symbol"
    )
    replacement.nodes[foreign.id] = foreign.model_copy(update={"repo_id": "B"})
    with pytest.raises(ValueError, match="owned"):
        store.publish_repository(replacement, repo_id="B")
    assert store.load() is old


@pytest.mark.parametrize("order", [("A", "B"), ("B", "A")])
def test_independently_prepared_deltas_preserve_other_repos_inputs(order):
    from axon.v1.graph import GraphSnapshot
    from axon.v1.graph_delta import GraphDelta

    empty = GraphSnapshot()
    deltas = {
        "A": GraphDelta.between(empty, caller()),
        "B": GraphDelta.between(empty, repository("B", "ledger")),
    }
    store = MemoryStore()
    first_a = None
    for repo in order:
        store.apply_delta(deltas[repo])
        if repo == "A":
            first_a = store.load().link_inputs["A"]
    current = store.load()
    assert first_a is not None
    assert current.link_inputs["A"] == first_a
    assert len(current.link_inputs["A"].prepared) == 3
    assert not current.link_inputs["A"].pinned
    assert outbound(current)[10].to_id == endpoint_node_id("B", "ledger", "GET", "/entries")


@pytest.mark.parametrize("method", ["publish", "save", "delta"])
def test_unowned_external_cannot_replace_retained_endpoint(method):
    from axon.v1.graph_delta import GraphDelta
    from axon.v1.models import GraphNode, NodeKind

    store = MemoryStore()
    store.save(caller())
    old = store.load()
    endpoint = next(node for node in old.nodes.values() if node.kind == NodeKind.API_ENDPOINT)
    incoming = repository("B", "ledger")
    incoming.add_node(GraphNode(id=endpoint.id, kind=NodeKind.EXTERNAL_API))
    with pytest.raises(ValueError, match="owned"):
        if method == "publish":
            store.publish_repository(incoming, repo_id="B")
        elif method == "save":
            store.save(incoming)
        else:
            store.apply_delta(GraphDelta(upsert_nodes=incoming.nodes))
    assert store.load() is old
    assert store.load().nodes[endpoint.id] is endpoint


def test_delta_rebases_unchanged_stale_input_against_latest_publication():
    from copy import deepcopy

    from axon.v1.graph_delta import GraphDelta, replace_repositories

    store = MemoryStore()
    store.save(caller())
    base = store.load()
    stale_target = replace_repositories(base, repository("B", "ledger"))
    delta = GraphDelta.between(base, stale_target)
    updated = deepcopy(caller())
    updated.link_inputs["A"].source_commit = "new-A"
    store.publish_repository(updated, repo_id="A")
    latest_a = store.load().link_inputs["A"]
    store.apply_delta(delta)
    assert store.load().link_inputs["A"] == latest_a
    assert outbound(store.load())[10].to_id == endpoint_node_id("B", "ledger", "GET", "/entries")


def test_input_only_delta_replaces_explicitly_changed_input():
    from copy import deepcopy

    from axon.v1.graph_delta import GraphDelta

    store = MemoryStore()
    store.save(caller())
    old = store.load()
    target = deepcopy(old)
    target.link_inputs["A"].source_commit = "new-commit"
    delta = GraphDelta.between(old, target)
    store.apply_delta(delta)
    assert store.load().link_inputs["A"].source_commit == "new-commit"
    assert store.load().link_inputs["A"].source_generation != old.link_inputs["A"].source_generation


@pytest.mark.parametrize("kind", ["ExternalApi", "Symbol"])
def test_delta_cannot_overwrite_repo_marker_to_remove_a_repository(kind):
    from axon.v1.graph_delta import GraphDelta
    from axon.v1.models import GraphNode, NodeKind

    store = MemoryStore()
    store.save(caller())
    old = store.load()
    repo = next(node for node in old.nodes.values() if node.kind == NodeKind.REPO)
    replacement = repository("B", "ledger")
    replacement.add_node(GraphNode(id=repo.id, kind=kind, repo_id="B" if kind == "Symbol" else ""))
    with pytest.raises(ValueError, match="owned"):
        store.apply_delta(GraphDelta(upsert_nodes=replacement.nodes))
    assert store.load() is old
