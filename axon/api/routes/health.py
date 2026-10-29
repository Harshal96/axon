from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from axon.db.connection import verify_connectivity
from axon.db.queries import get_graph_stats, get_latest_commit_snapshot

router = APIRouter()


class GraphVersionInfo(BaseModel):
    sha: str | None = None
    indexed_at: datetime | None = None
    is_stale: bool = False
    stale_by_seconds: int | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "unhealthy"]
    neo4j: Literal["connected", "unreachable"]
    graph_version: GraphVersionInfo | None = None
    graph_stats: dict | None = None
    timestamp: datetime


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """
    Returns the operational status of the axon service and its
    connection to Neo4j. Also reports the latest indexed commit SHA and
    whether the graph is stale.
    """
    neo4j_up = await verify_connectivity()

    graph_version: GraphVersionInfo | None = None
    graph_stats: dict | None = None

    if neo4j_up:
        try:
            latest = await get_latest_commit_snapshot()
            stats = await get_graph_stats()
            graph_stats = stats

            if latest:
                indexed_at = latest.get("index_completed_at")
                stale_by = None
                is_stale = False
                if indexed_at:
                    now = datetime.now(tz=UTC)
                    if indexed_at.tzinfo is None:
                        indexed_at = indexed_at.replace(tzinfo=UTC)
                    stale_by = int((now - indexed_at).total_seconds())
                    is_stale = stale_by > 300  # 5 min threshold

                graph_version = GraphVersionInfo(
                    sha=latest.get("sha"),
                    indexed_at=indexed_at,
                    is_stale=is_stale,
                    stale_by_seconds=stale_by,
                )
            else:
                graph_version = GraphVersionInfo()  # no commit indexed yet

        except Exception:
            pass  # Stats failure doesn't degrade health status

    status: Literal["ok", "degraded", "unhealthy"] = "ok" if neo4j_up else "unhealthy"

    return HealthResponse(
        status=status,
        neo4j="connected" if neo4j_up else "unreachable",
        graph_version=graph_version,
        graph_stats=graph_stats,
        timestamp=datetime.now(tz=UTC),
    )
