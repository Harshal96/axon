"""
Tests for the /api/v1/graph/* endpoints.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


def _mock_session_router():
    mock_session = AsyncMock()

    async def mock_run(query, **kwargs):
        mock_result = AsyncMock()
        mock_result.data = AsyncMock(return_value=[])
        mock_result.single = AsyncMock(return_value=None)
        return mock_result

    mock_session.run = mock_run
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.asyncio
async def test_list_targets_empty(async_client: AsyncClient) -> None:
    """The endpoint should return successfully even with no targets."""
    with patch("axon.api.routes.graph.async_session", return_value=_mock_session_router()):
        response = await async_client.get("/api/v1/graph/targets")
    assert response.status_code == 200
    data = response.json()
    assert "total" in data
    assert "targets" in data
    assert isinstance(data["targets"], list)


@pytest.mark.asyncio
async def test_list_targets_with_filter(async_client: AsyncClient) -> None:
    """Filtering by a non-existent package should return zero targets."""
    with patch("axon.api.routes.graph.async_session", return_value=_mock_session_router()):
        response = await async_client.get(
            "/api/v1/graph/targets", params={"package": "//nonexistent/package"}
        )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["targets"] == []


@pytest.mark.asyncio
async def test_get_target_not_found(async_client: AsyncClient) -> None:
    """Fetching a non-existent target should return 404."""
    with patch("axon.api.routes.graph.async_session", return_value=_mock_session_router()):
        response = await async_client.get("/api/v1/graph/targets/nonexistent:target")
    assert response.status_code == 404
