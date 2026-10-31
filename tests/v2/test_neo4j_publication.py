"""Opt-in publication checks against a dedicated Neo4j 5 test database.

Set AXON_TEST_NEO4J_URI, AXON_TEST_NEO4J_USER and AXON_TEST_NEO4J_PASSWORD.
The test migrates Axon constraints; never point it at a production database.
"""

from __future__ import annotations

import os
import uuid

import pytest

from axon.v2.graph import GraphSnapshot
from axon.v2.models import GraphEdge, GraphNode, NodeKind
from axon.v2.store import Neo4jStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("AXON_TEST_NEO4J_URI"), reason="dedicated Neo4j test database not configured"
)


@pytest.fixture
def database_store(monkeypatch):
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        os.environ["AXON_TEST_NEO4J_URI"],
        auth=(os.environ["AXON_TEST_NEO4J_USER"], os.environ["AXON_TEST_NEO4J_PASSWORD"]),
    )
    store = Neo4jStore()
    monkeypatch.setattr(store, "_session", driver.session)
    monkeypatch.setattr("axon.v2.store.NEO4J_BATCH_SIZE", 2)
    store.ensure_schema()
    repo = "publication-test-" + uuid.uuid4().hex
    try:
        yield store, repo, driver
    finally:
        with driver.session() as session:
            for kind in NodeKind:
                session.run(
                    f"MATCH (n:{kind.value}) WHERE n.repo_id = $repo OR n._axon_owner = $repo "
                    "DETACH DELETE n",
                    repo=repo,
                ).consume()
            session.run("MATCH (p:_AxonPublication {repo_id: $repo}) DELETE p", repo=repo).consume()
        driver.close()


def test_bounded_publication_delta_and_interrupted_staging(database_store, monkeypatch):
    store, repo, driver = database_store

    def snapshot(prefix):
        nodes = {
            f"{repo}:{prefix}:{i}": GraphNode(
                id=f"{repo}:{prefix}:{i}", kind=NodeKind.SYMBOL, repo_id=repo, name=f"{prefix}-{i}"
            )
            for i in range(5)
        }
        ids = list(nodes)
        return GraphSnapshot(
            nodes=nodes, edges=[GraphEdge(from_id=ids[0], to_id=ids[1], kind="CALLS")]
        )

    initial = snapshot("first")
    store.save(initial)
    reader = Neo4jStore()
    monkeypatch.setattr(reader, "_session", driver.session)
    assert set(reader.load().nodes) >= initial.nodes.keys()
    changed = initial.model_copy(deep=True)
    changed.nodes[next(iter(changed.nodes))].name = "updated"
    store.save(changed)
    reader._cached = None
    assert all(reader.load().nodes[key] == value for key, value in changed.nodes.items())

    original_batch = store._run_batch

    def interrupt(session, query, **kwargs):
        if kwargs["label"] == "publication":
            raise RuntimeError("simulated interruption before publication")
        original_batch(session, query, **kwargs)

    monkeypatch.setattr(store, "_run_batch", interrupt)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        store.save(snapshot("second"))
    reader._cached = None
    repo_nodes = {key for key, node in reader.load().nodes.items() if node.repo_id == repo}
    assert repo_nodes == changed.nodes.keys()
    rows = reader.run_cypher(
        f"MATCH (n:Symbol {{repo_id: '{repo}'}}) RETURN n.name AS name ORDER BY name"
    )
    assert sorted(row["name"] for row in rows) == sorted(n.name for n in changed.nodes.values())
