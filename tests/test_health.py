from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_returns_ok(async_client: AsyncClient) -> None:
    with (
        patch("axon.api.routes.health.verify_connectivity", new=AsyncMock(return_value=True)),
        patch("axon.api.routes.health.get_graph_stats", new=AsyncMock(return_value={"nodes": 10})),
        patch(
            "axon.api.routes.health.get_latest_commit_snapshot",
            new=AsyncMock(return_value={"sha": "abc", "index_completed_at": None}),
        ),
    ):
        response = await async_client.get("/api/v1/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["neo4j"] == "connected"
    assert "timestamp" in data
    assert "graph_version" in data


@pytest.mark.asyncio
async def test_health_graph_version_shape(async_client: AsyncClient) -> None:
    with (
        patch("axon.api.routes.health.verify_connectivity", new=AsyncMock(return_value=True)),
        patch("axon.api.routes.health.get_graph_stats", new=AsyncMock(return_value={"nodes": 10})),
        patch(
            "axon.api.routes.health.get_latest_commit_snapshot",
            new=AsyncMock(return_value={"sha": "abc", "index_completed_at": None}),
        ),
    ):
        response = await async_client.get("/api/v1/health")

    data = response.json()
    gv = data["graph_version"]
    assert gv is not None
    assert gv["sha"] == "abc"
