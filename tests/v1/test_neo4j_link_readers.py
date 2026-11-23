"""Opt-in reader syntax/visibility checks on the dedicated disposable test DB.

Uses AXON_TEST_NEO4J_URI/USER/PASSWORD, as test_neo4j_publication does. This test
owns a temporary global link marker: no existing global marker is overwritten.
"""

from __future__ import annotations

import os
import uuid

import pytest

from axon.v1.store import Neo4jStore, scope_active_generations

pytestmark = pytest.mark.skipif(
    not os.environ.get("AXON_TEST_NEO4J_URI"), reason="dedicated Neo4j test database not configured"
)


def test_link_generation_visibility_legacy_migration_and_query_syntax(monkeypatch):
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        os.environ["AXON_TEST_NEO4J_URI"],
        auth=(os.environ["AXON_TEST_NEO4J_USER"], os.environ["AXON_TEST_NEO4J_PASSWORD"]),
    )
    tag = "link-readers-" + uuid.uuid4().hex
    store = Neo4jStore()
    monkeypatch.setattr(store, "_session", driver.session)
    try:
        with driver.session() as session:
            existing = session.run("MATCH (p:_AxonLinkPublication {id:'global'}) RETURN p").single()
            assert existing is None
            session.run(
                "CREATE (a:Symbol {id:$tag + ':caller', repo_id:$tag, "
                "_axon_storage_id:'private-id'}), "
                "(b:ApiEndpoint {id:$tag + ':target', repo_id:$tag}), "
                "(legacy:ExternalApi {id:$tag + ':legacy', repo_id:''}), "
                "(active:ExternalApi {id:$tag + ':active', repo_id:'', "
                "_axon_link_generation:$tag}), "
                "(staged:ExternalApi {id:$tag + ':staged', repo_id:'', "
                "_axon_link_generation:'staged'}), "
                "(private:_AxonLinkInput {id:$tag + ':private', repo_id:$tag}), "
                "(a)-[:CALLS_API {name:'legacy'}]->(b), "
                "(a)-[:CALLS_API {name:'active', _axon_link_generation:$tag}]->(b), "
                "(a)-[:CALLS_API {name:'staged', _axon_link_generation:'staged'}]->(b), "
                "(a)-[:CALLS {name:'structural', _axon_link_generation:'staged'}]->(b)",
                tag=tag,
            ).consume()

            def names(pattern, where=""):
                query = f"MATCH {pattern} {where} RETURN r.name AS name ORDER BY name"
                return [r["name"] for r in session.run(scope_active_generations(query), tag=tag)]

            assert names("(a {id:$tag + ':caller'})-[r:CALLS_API]->(b)") == ["legacy"]
            assert names("(a {id:$tag + ':caller'})-[r]->(b)") == ["legacy", "structural"]
            before = store.load()
            assert tag + ":legacy" in before.nodes
            assert tag + ":active" not in before.nodes
            assert tag + ":staged" not in before.nodes
            assert tag + ":private" not in before.nodes
            assert sorted(
                e.props["name"] for e in before.edges if e.from_id == tag + ":caller"
            ) == [
                "legacy",
                "structural",
            ]

            session.run(
                "CREATE (:_AxonLinkPublication {id:'global', generation:$tag})", tag=tag
            ).consume()
            assert names("(a {id:$tag + ':caller'})-[r:CALLS_API]->(b)") == ["active"]
            assert names("(a {id:$tag + ':caller'})-[r]->(b)") == ["active", "structural"]
            assert (
                names(
                    "(a {id:$tag + ':caller'})-[r:CALLS_API]->(b)",
                    "WHERE r.name = 'legacy' OR r.name = 'staged'",
                )
                == []
            )
            for connector in ("-[:CALLS_API]->", "-->", "-[]->", "<--", "--"):
                query = (
                    "MATCH (a {id:$tag + ':caller'}) OPTIONAL MATCH (a)"
                    + connector
                    + "(b) WHERE b.id = $tag + ':absent' OR b.id = 'unknown' "
                    "RETURN a.id AS caller, b.id AS target"
                )
                rows = list(session.run(scope_active_generations(query), tag=tag))
                assert len(rows) == 1 and rows[0]["target"] is None
            optional_named = (
                "MATCH (a {id:$tag + ':caller'}) "
                "OPTIONAL MATCH (a)-[r:CALLS_API]->(b) "
                "WHERE r.name = 'legacy' OR r.name = 'staged' "
                "RETURN a.id AS caller, r.name AS target"
            )
            rows = list(session.run(scope_active_generations(optional_named), tag=tag))
            assert len(rows) == 1 and rows[0]["target"] is None
            anonymous = (
                "MATCH ({id:$tag + ':caller'})-[:CALLS_API]->({id:$tag + ':target'}) "
                "RETURN count(*) AS total"
            )
            assert session.run(scope_active_generations(anonymous), tag=tag).single()["total"] == 1
            internal = "MATCH (n) WHERE n.repo_id = $tag RETURN labels(n) AS labels"
            assert all(
                not label.startswith("_Axon")
                for rec in session.run(scope_active_generations(internal), tag=tag)
                for label in rec["labels"]
            )
            for projection in (
                "properties(a) AS node_props, properties(r) AS edge_props",
                "a AS node, r AS edge",
                "{nested:[a, r, properties(a), properties(r)]} AS nested",
                "p AS path, nodes(p) AS nodes, relationships(p) AS edges",
            ):
                rows = store.run_cypher(
                    "MATCH p=(a {id:'" + tag + ":caller'})-[r:CALLS_API]->(b) RETURN " + projection
                )
                assert len(rows) == 1
                assert "_axon_" not in str(rows) and "active" in str(rows)
            projection_id = store.run_cypher(
                "MATCH (a {id:'" + tag + ":caller'}) RETURN a {nested:a {.*}}['nested']['id'] AS id"
            )
            assert projection_id == [{"id": tag + ":caller"}]
            for access in (
                "a._axon_generation",
                "a['_axon_generation']",
                "keys(a)",
                "a {.*}['_axon_storage_id']",
                "a {.*}['_' + 'axon_storage_id']",
            ):
                with pytest.raises(ValueError, match="generation-safe"):
                    store.run_cypher("MATCH (a) RETURN " + access + " AS value")
            with pytest.raises(ValueError, match="generation-safe"):
                store.run_cypher(
                    "WITH '_axon_storage_id' AS key MATCH (a) RETURN a {.*}[key] AS value"
                )
            store._cached = None
            after = store.load()
            assert tag + ":legacy" not in after.nodes
            assert tag + ":active" in after.nodes
            assert tag + ":staged" not in after.nodes
            assert all(not k.startswith("_axon_") for n in after.nodes.values() for k in n.props)
            assert sorted(e.props["name"] for e in after.edges if e.from_id == tag + ":caller") == [
                "active",
                "structural",
            ]
    finally:
        with driver.session() as session:
            session.run("MATCH (n) WHERE n.id STARTS WITH $tag DETACH DELETE n", tag=tag).consume()
            session.run(
                "MATCH (p:_AxonLinkPublication {id:'global', generation:$tag}) DELETE p", tag=tag
            ).consume()
        driver.close()
