"""Health of the configured API-first graph store."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from axon.config import get_settings
from axon.db.connection import verify_connectivity

router = APIRouter()


@router.get("/health")
async def health_check(request: Request) -> dict[str, Any]:
    persistent = get_settings().axon_store == "neo4j"
    connected = await verify_connectivity() if persistent else True
    stats = None
    if connected:
        try:
            snapshot = await asyncio.to_thread(request.app.state.v1_store.load)
            stats = {"nodes": len(snapshot.nodes), "edges": len(snapshot.edges)}
        except Exception:
            connected = False
    return {
        "status": "ok" if connected else "unhealthy",
        "neo4j": ("connected" if connected else "unreachable") if persistent else "disabled",
        "graph_stats": stats,
        "timestamp": datetime.now(UTC),
    }
