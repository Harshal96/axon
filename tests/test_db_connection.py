from __future__ import annotations

from axon.db.connection import get_sync_driver, sync_session


def test_sync_driver_connects() -> None:
    """The sync driver should be able to verify connectivity to Neo4j."""
    driver = get_sync_driver()
    driver.verify_connectivity()


def test_sync_session_runs_query() -> None:
    """A basic Cypher query should succeed through the sync session."""
    with sync_session() as session:
        result = session.run("RETURN 1 AS n")
        record = result.single()
        assert record is not None
        assert record["n"] == 1
