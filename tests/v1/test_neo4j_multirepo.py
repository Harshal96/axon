"""Opt-in lifecycle qualification; only the disposable dedicated Neo4j is allowed."""

import json
import os
from pathlib import Path

import pytest

from axon.v1.graph import GraphSnapshot
from axon.v1.graph_delta import GraphDelta
from axon.v1.models import GraphEdge, GraphNode, NodeKind, endpoint_node_id
from axon.v1.store import Neo4jStore, load_snapshot_file, save_snapshot_file
from tests.v1.test_multirepo_linking import caller, outbound, repository

pytestmark = pytest.mark.skipif(
    not os.environ.get("AXON_TEST_NEO4J_URI"), reason="dedicated Neo4j not configured"
)


@pytest.fixture
def live(monkeypatch, tmp_path):
    from neo4j import GraphDatabase

    assert os.environ["AXON_TEST_NEO4J_URI"] == "bolt://localhost:17687"
    driver = GraphDatabase.driver(
        os.environ["AXON_TEST_NEO4J_URI"],
        auth=(os.environ["AXON_TEST_NEO4J_USER"], os.environ["AXON_TEST_NEO4J_PASSWORD"]),
    )
    with driver.session() as session:
        assert session.run("MATCH (n) RETURN count(n) AS count").single()["count"] == 0
    monkeypatch.setattr("axon.v1.store.NEO4J_BATCH_SIZE", 2)
    monkeypatch.setenv("AXON_STATE_DIR", str(tmp_path))

    def store():
        writer = Neo4jStore()
        monkeypatch.setattr(writer, "_session", driver.session)
        return writer

    writer = store()
    writer.ensure_schema()
    try:
        yield writer, store, driver, tmp_path
    finally:
        with driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n").consume()
        driver.close()


def agree(writer, fresh, tmp_path):
    cached, cold = writer.load(), fresh().load()
    assert cold.nodes == cached.nodes
    assert sorted(
        json.dumps(e.model_dump(mode="json"), sort_keys=True) for e in cold.edges
    ) == sorted(json.dumps(e.model_dump(mode="json"), sort_keys=True) for e in cached.edges)
    assert cold.link_inputs == cached.link_inputs
    assert cold.link_reports == cached.link_reports
    assert cold.unresolved == cached.unresolved
    assert cold.diagnostics == cached.diagnostics
    assert cold.publication_id == cached.publication_id
    assert cold.publication_stats.staged_rows == 0
    assert cached.publication_stats.staged_rows > 0
    path = tmp_path / "roundtrip.json"
    save_snapshot_file(cold, path)
    assert load_snapshot_file(path).model_dump() == cold.model_dump()
    rows = writer.run_cypher("MATCH (a)-[r:CALLS_API]->(b) RETURN a.id AS caller, b.id AS target")
    assert {(row["caller"], row["target"]) for row in rows} == {
        (e.from_id, e.to_id) for e in cold.edges if e.kind.value == "CALLS_API"
    }


@pytest.mark.parametrize("order", [("A", "B"), ("B", "A")])
def test_live_replay_restart_target_updates_import_and_removal(live, order):
    writer, fresh, driver, tmp_path = live
    snapshots = {"A": caller(), "B": repository("B", "ledger")}
    for repo in order:
        writer.publish_repository(snapshots[repo], repo_id=repo)
        agree(writer, fresh, tmp_path)
    old = writer.load()
    a_generation = old.link_inputs["A"].source_generation
    writer = fresh()  # no caller checkout/cache involved
    writer.save(repository("B", "ledger", endpoint=False))
    assert writer.load().link_inputs["A"].source_generation == a_generation
    assert outbound(writer.load())[10].to_id != endpoint_node_id("B", "ledger", "GET", "/entries")
    agree(writer, fresh, tmp_path)
    writer.rollback_generation("B")
    assert outbound(writer.load())[10].to_id == endpoint_node_id("B", "ledger", "GET", "/entries")
    agree(writer, fresh, tmp_path)
    writer.remove_repository("B")
    agree(writer, fresh, tmp_path)
    with driver.session() as session:
        marker = session.run(
            "MATCH (p:_AxonPublication {repo_id:'B'}) RETURN properties(p) AS p"
        ).single()["p"]
    assert marker["deleted"]
    writer.save(caller())  # must preserve removed B's retained source pair
    writer.rollback_generation("B")
    assert outbound(writer.load())[10].to_id == endpoint_node_id("B", "ledger", "GET", "/entries")
    agree(writer, fresh, tmp_path)
    print(
        "PUBLICATION_METRICS", json.dumps(writer.load().publication_stats.__dict__, sort_keys=True)
    )


def test_live_failure_staged_links_inputs_reports_and_unchanged_structure(live, monkeypatch):
    writer, fresh, driver, tmp_path = live
    writer.save(caller())
    writer.save(repository("B", "ledger"))
    old = writer.load()
    original = writer._run_batch
    staged = []

    def fail(session, query, **kwargs):
        assert len(kwargs["rows"]) <= 2
        if kwargs["label"] == "publication":
            staged.append(True)
            # All staged metadata/projection still hidden, even though A's source
            # and its physical rows have not changed during target-only replay.
            agree(writer, fresh, tmp_path)
            raise RuntimeError("forced before marker switch")
        original(session, query, **kwargs)

    monkeypatch.setattr(writer, "_run_batch", fail)
    with pytest.raises(RuntimeError, match="forced"):
        writer.save(repository("B", "ledger", endpoint=False))
    assert staged and writer.load() is old
    agree(writer, fresh, tmp_path)
    with pytest.raises(RuntimeError, match="forced"):
        writer.save(repository("C", "other"))  # A and B endpoints both unchanged
    assert writer.load() is old
    agree(writer, fresh, tmp_path)
    monkeypatch.setattr(writer, "_run_batch", original)
    # Input-only replacement reprojects links between unchanged source nodes.
    changed = old.model_copy(deep=True)
    changed.link_inputs["A"].prepared[0].fact.url = "https://unknown.example/new"
    writer.apply_delta(GraphDelta.between(old, changed))
    agree(writer, fresh, tmp_path)
    # Cancellation before switch preserves the last publication.
    current = writer.load()

    def cancel(done, total):
        if done < total:
            raise RuntimeError("cancelled staging")

    writer.publish_progress = cancel
    with pytest.raises(RuntimeError, match="cancelled staging"):
        writer.save(caller())
    assert writer.load() is current
    writer.publish_progress = lambda done, total: (
        (_ for _ in ()).throw(RuntimeError("postcommit")) if done == total else None
    )
    writer.save(repository("B", "ledger"))
    agree(writer, fresh, tmp_path)


def test_live_legacy_sdk_bootstrap_safety_and_rollback(live):
    writer, fresh, driver, tmp_path = live
    legacy = GraphSnapshot(
        schema_version=1,
        nodes={
            "repo::A": GraphNode(id="repo::A", kind=NodeKind.REPO, repo_id="A"),
            "symbol::A": GraphNode(id="symbol::A", kind=NodeKind.SYMBOL, repo_id="A"),
            "repo::B": GraphNode(id="repo::B", kind=NodeKind.REPO, repo_id="B"),
            "legacy-api": GraphNode(
                id="legacy-api",
                kind=NodeKind.API_ENDPOINT,
                repo_id="B",
                service_id="ledger",
                props={"method": "GET", "path": "/legacy"},
            ),
            "sdk": GraphNode(
                id="sdk",
                kind=NodeKind.EXTERNAL_API,
                name="PutObject",
                props={"kind": "sdk", "provider": "aws.s3", "operation": "put_object"},
            ),
        },
        edges=[
            GraphEdge(from_id="symbol::A", to_id="sdk", kind="CALLS_API"),
            GraphEdge(from_id="symbol::A", to_id="legacy-api", kind="CALLS_API"),
        ],
    )
    with driver.session() as session:
        for node in legacy.nodes.values():
            session.run(
                f"CREATE (n:{node.kind.value}) SET n=$props",
                props={"id": node.id, "repo_id": node.repo_id, "name": node.name, **node.props},
            ).consume()
        for edge in legacy.edges:
            session.run(
                "MATCH (a {id:$a}), (b {id:$b}) CREATE (a)-[:CALLS_API]->(b)",
                a=edge.from_id,
                b=edge.to_id,
            ).consume()
    writer.save(repository("B", "ledger"))
    safety = Path(writer.last_safety_snapshot)
    assert safety.exists() and json.loads(safety.read_text())["schema_version"] == 1
    assert writer.load().nodes["sdk"].props["provider"] == "aws.s3"
    agree(writer, fresh, tmp_path)
    replacement = GraphSnapshot(
        nodes={
            "repo::A": legacy.nodes["repo::A"],
            "new": GraphNode(id="new", kind=NodeKind.SYMBOL, repo_id="A"),
        }
    )
    writer.save(replacement)
    assert "sdk" not in writer.load().nodes
    writer.save(repository("B", "ledger", endpoint=False))
    writer = fresh()
    writer.rollback_generation("A")
    assert writer.load().nodes["sdk"].props == legacy.nodes["sdk"].props
    assert "new" not in writer.load().nodes
    assert "legacy-api" not in writer.load().nodes
    assert any(
        n.kind == NodeKind.EXTERNAL_API and n.props.get("target_endpoint") == "legacy-api"
        for n in writer.load().nodes.values()
    )
    agree(writer, fresh, tmp_path)


def test_live_atomic_legacy_input_bootstrap_reuses_physical_source(live, monkeypatch):
    writer, fresh, driver, tmp_path = live
    with driver.session() as session:
        session.run(
            "CREATE (p:_AxonPublication {repo_id:'A',generation:'legacy-source',schema_version:1})"
        ).consume()
        session.run(
            "CREATE (n:Repo "
            "{id:'repo::A',repo_id:'A',_axon_owner:'A',_axon_generation:'legacy-source',"
            "_axon_storage_id:'legacy-source:repo::A'})"
        ).consume()
    old = writer.load()
    original = writer._run_batch

    def fail(session, query, **kwargs):
        if kwargs["label"] == "publication":
            cold = fresh().load()
            assert cold.schema_version == 1 and cold.link_inputs == {}
            with driver.session() as check:
                props = check.run(
                    "MATCH (p:_AxonPublication {repo_id:'A'}) RETURN properties(p) AS p"
                ).single()["p"]
            assert "input_generation" not in props
            raise RuntimeError("bootstrap interrupted")
        original(session, query, **kwargs)

    monkeypatch.setattr(writer, "_run_batch", fail)
    with pytest.raises(RuntimeError, match="bootstrap interrupted"):
        writer.save(repository("B", "ledger"))
    assert writer.load() is old
    monkeypatch.setattr(writer, "_run_batch", original)
    writer.save(repository("B", "ledger"))
    assert writer.load().link_inputs["A"].source_generation == "legacy-source"
    with driver.session() as check:
        assert (
            check.run("MATCH (n:Repo {id:'repo::A'}) RETURN count(n) AS count").single()["count"]
            == 1
        )
    agree(writer, fresh, tmp_path)


def test_live_stale_deltas_empty_replacement_and_validation(live):
    writer, fresh, driver, tmp_path = live
    empty = GraphSnapshot()
    a, b = GraphDelta.between(empty, caller()), GraphDelta.between(empty, repository("B", "ledger"))
    writer.apply_delta(b)
    writer.apply_delta(a)
    agree(writer, fresh, tmp_path)
    old = writer.load()

    def reject(candidate):
        assert "C" in candidate.link_inputs
        raise ValueError("validation rejected")

    with pytest.raises(ValueError, match="validation rejected"):
        writer.publish_repository(repository("C", "ledger"), repo_id="C", validate=reject)
    assert writer.load() is old
    writer.save(
        GraphSnapshot(nodes={"repo::A": GraphNode(id="repo::A", kind=NodeKind.REPO, repo_id="A")})
    )
    assert not any(e.kind.value == "CALLS_API" for e in writer.load().edges)
    agree(writer, fresh, tmp_path)


def test_live_activation_retains_previous_legacy_generation_links(live, monkeypatch):
    from axon.v1.models import Evidence

    writer, fresh, driver, tmp_path = live
    sdk = GraphNode(
        id="old-sdk",
        kind=NodeKind.EXTERNAL_API,
        name="Old SDK",
        props={"kind": "sdk", "provider": "old-provider", "operation": "old_operation"},
    )
    generic = GraphNode(
        id="old-http",
        kind=NodeKind.EXTERNAL_API,
        name="Old HTTP",
        props={"method": "GET", "url": "https://old.example/api", "custom": "retained"},
    )
    new_sdk = GraphNode(
        id="new-sdk",
        kind=NodeKind.EXTERNAL_API,
        name="Current SDK",
        props={"kind": "sdk", "provider": "new-provider"},
    )
    historical_edges = [
        GraphEdge(
            from_id="old-helper",
            to_id=target,
            kind="CALLS_API",
            confidence="INFERRED",
            evidence=Evidence(
                file="old.py", line=line, rule="legacy-rule", detail="retained detail"
            ),
            props={"legacy": "kept"},
        )
        for line, target in enumerate([sdk.id, generic.id, "remote-api", "inactive-api"], 10)
    ]
    with driver.session() as session:
        for repo, current, previous in [("A", "G2", "G1"), ("C", "C2", "C1")]:
            session.run(
                "CREATE (p:_AxonPublication) SET p=$props",
                props={
                    "repo_id": repo,
                    "generation": current,
                    "prev_generation": previous,
                    "schema_version": 1,
                },
            ).consume()
        for repo, generation in [("A", "G1"), ("A", "G2"), ("C", "C1"), ("C", "C2")]:
            nodes = [GraphNode(id=f"repo::{repo}", kind=NodeKind.REPO, repo_id=repo)]
            if repo == "A":
                nodes.append(
                    GraphNode(id="caller-A", kind=NodeKind.SYMBOL, repo_id="A", name=generation)
                )
                if generation == "G1":
                    nodes.append(GraphNode(id="old-helper", kind=NodeKind.SYMBOL, repo_id="A"))
            else:
                nodes.append(
                    GraphNode(
                        id="remote-api",
                        kind=NodeKind.API_ENDPOINT,
                        repo_id="C",
                        service_id="remote",
                        name=generation,
                        props={"method": "GET", "path": "/api"},
                    )
                )
                if generation == "C1":
                    nodes.append(
                        GraphNode(
                            id="inactive-api",
                            kind=NodeKind.API_ENDPOINT,
                            repo_id="C",
                            service_id="remote",
                            props={"method": "GET", "path": "/removed"},
                        )
                    )
            for node in nodes:
                row = writer._node_row(node, f"{generation}:{node.id}", repo, generation)
                session.run(
                    f"CREATE (n:{node.kind.value}) SET n=$props", props=row["props"]
                ).consume()
        for node in (sdk, generic, new_sdk):
            row = writer._node_row(node, node.id, "", "")
            session.run("CREATE (n:ExternalApi) SET n=$props", props=row["props"]).consume()
        physical = {
            sdk.id: sdk.id,
            generic.id: generic.id,
            "old-helper": "G1:old-helper",
            "remote-api": "C1:remote-api",
            "inactive-api": "C1:inactive-api",
        }
        for edge in historical_edges:
            row = writer._edge_row(edge, physical)
            session.run(
                "MATCH (a {_axon_storage_id:$a}), (b {_axon_storage_id:$b}) "
                "CREATE (a)-[r:CALLS_API]->(b) SET r=$props",
                **row,
            ).consume()
        session.run(
            "MATCH (a {_axon_storage_id:'G1:caller-A'}), "
            "(b {_axon_storage_id:'G1:old-helper'}) CREATE (a)-[:CALLS]->(b)"
        ).consume()
        session.run(
            "MATCH (a {_axon_storage_id:'G2:caller-A'}), "
            "(b {_axon_storage_id:'new-sdk'}) CREATE (a)-[:CALLS_API]->(b)"
        ).consume()
    old = writer.load()
    assert {e.to_id for e in old.edges if e.kind.value == "CALLS_API"} == {new_sdk.id}
    physical_before, generations_before = dict(writer._physical_ids), dict(writer._generations)
    inputs = writer._legacy_generation_inputs("A", "G1")
    assert len(inputs.pinned) == 4
    assert writer._physical_ids == physical_before and writer._generations == generations_before
    assert writer.load() is old
    original = writer._run_batch

    def interrupt(session, query, **kwargs):
        if kwargs["label"] == "publication":
            cold = fresh().load()
            assert cold.link_inputs == {}
            assert {e.to_id for e in cold.edges if e.kind.value == "CALLS_API"} == {new_sdk.id}
            raise RuntimeError("previous bootstrap interrupted")
        original(session, query, **kwargs)

    monkeypatch.setattr(writer, "_run_batch", interrupt)
    with pytest.raises(RuntimeError, match="previous bootstrap interrupted"):
        writer.save(repository("B", "ledger"))
    assert writer.load() is old
    monkeypatch.setattr(writer, "_run_batch", original)
    writer.save(repository("B", "ledger"))
    assert writer.load().link_inputs["A"].source_generation == "G2"
    assert {e.to_id for e in writer.load().edges if e.kind.value == "CALLS_API"} == {new_sdk.id}
    assert not {"old-helper", "inactive-api", "old-sdk", "old-http"} & writer.load().nodes.keys()
    assert writer.load().nodes["remote-api"].name == "C2"
    agree(writer, fresh, tmp_path)
    writer = fresh()
    writer.rollback_generation("A")
    restored = writer.load()
    assert restored.nodes["caller-A"].name == "G1"
    assert restored.nodes[sdk.id].props == sdk.props
    assert restored.nodes[generic.id].props == generic.props
    assert restored.nodes["remote-api"].name == "C2"
    assert "inactive-api" not in restored.nodes and "repo::B" in restored.nodes
    assert any(
        e.from_id == "caller-A" and e.to_id == "old-helper" and e.kind.value == "CALLS"
        for e in restored.edges
    )
    for previous in historical_edges:
        edge = next(
            e
            for e in restored.edges
            if e.from_id == previous.from_id and e.evidence.line == previous.evidence.line
        )
        assert edge.evidence == previous.evidence and edge.confidence == previous.confidence
        assert edge.props["legacy"] == "kept"
        if previous.to_id == "inactive-api":
            assert restored.nodes[edge.to_id].props["target_endpoint"] == "inactive-api"
        else:
            assert edge.to_id == previous.to_id
    agree(writer, fresh, tmp_path)
