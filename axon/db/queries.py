"""
Raw Cypher query helpers. All queries use parameterized inputs — never
string-interpolate user data into Cypher.
"""

from __future__ import annotations

from typing import Any

from axon.db.connection import async_session


async def get_node_by_id(node_id: str) -> dict | None:
    """Generic node fetch by the stable `id` property."""
    async with async_session() as session:
        result = await session.run(
            "MATCH (n {id: $id}) RETURN n",
            id=node_id,
        )
        record = await result.single()
        if record is None:
            return None
        return dict(record["n"])


async def node_exists(node_id: str) -> bool:
    async with async_session() as session:
        result = await session.run(
            "MATCH (n {id: $id}) RETURN count(n) AS cnt",
            id=node_id,
        )
        record = await result.single()
        return bool(record and record["cnt"] > 0)


async def get_graph_stats() -> dict[str, Any]:
    """Returns high-level graph statistics for the health endpoint."""
    async with async_session() as session:
        result = await session.run("""
            MATCH (n)
            RETURN
              count(n) AS node_count,
              count(DISTINCT labels(n)) AS label_count
        """)
        record = await result.single()
        if record is None:
            return {"node_count": 0, "label_count": 0}
        return {"node_count": record["node_count"], "label_count": record["label_count"]}


async def get_latest_commit_snapshot() -> dict | None:
    """Returns the most recently indexed CommitSnapshot."""
    async with async_session() as session:
        result = await session.run("""
            MATCH (c:CommitSnapshot {index_success: true})
            RETURN c
            ORDER BY c.index_completed_at DESC
            LIMIT 1
        """)
        record = await result.single()
        if record is None:
            return None
        return dict(record["c"])
