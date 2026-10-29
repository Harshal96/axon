"""
Tests for analysis operations: dead_code, hub_nodes, team_coupling.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from axon.graph.analysis import dead_code, hub_nodes, team_coupling
from axon.graph.models import GraphMeta


def _mock_session_router():
    mock_session = AsyncMock()

    async def mock_run(*args, **kwargs):
        query = args[0] if args else kwargs.get("query", "")
        mock_result = AsyncMock()
        records = []
        if "NOT (s)<-[:CALLS]-()" in query:
            records = [
                {
                    "name": "old_func",
                    "qualified_name": "mod.old",
                    "kind": "FUNCTION",
                    "file_path": "mod.py",
                    "line": 10,
                }
            ]
        elif "count(r) AS call_count" in query:
            records = [
                {
                    "name": "logger",
                    "qualified_name": "log.logger",
                    "kind": "FUNCTION",
                    "call_count": 100,
                    "file_path": "log.py",
                    "target_label": "//log:lib",
                }
            ]
        elif "from_team.name <> to_team.name" in query:
            records = [{"from_team": "team-a", "to_team": "team-b", "dep_count": 25}]

        mock_result.data = AsyncMock(return_value=records)
        mock_result.single = AsyncMock(return_value=records[0] if records else None)
        return mock_result

    mock_session.run = mock_run
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


def _mock_session_empty():
    mock_session = AsyncMock()
    mock_result = AsyncMock()
    mock_result.data = AsyncMock(return_value=[])
    mock_result.single = AsyncMock(return_value=None)
    mock_session.run = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_dead_code_returns_symbols() -> None:
    with patch("axon.graph.analysis.async_session", return_value=_mock_session_router()):
        resp = await dead_code("//a:a")
    assert resp.total == 1


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_dead_code_empty() -> None:
    with patch("axon.graph.analysis.async_session", return_value=_mock_session_empty()):
        resp = await dead_code("//a:a")
    assert resp.total == 0


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_hub_nodes_ordered_by_call_count() -> None:
    with patch("axon.graph.analysis.async_session", return_value=_mock_session_router()):
        resp = await hub_nodes(top_n=10)
    assert len(resp.hub_nodes) == 1


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_hub_nodes_empty() -> None:
    with patch("axon.graph.analysis.async_session", return_value=_mock_session_empty()):
        resp = await hub_nodes()
    assert len(resp.hub_nodes) == 0


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_team_coupling_matrix() -> None:
    with patch("axon.graph.analysis.async_session", return_value=_mock_session_router()):
        resp = await team_coupling()
    assert resp.total_cross_team_deps == 25


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_team_coupling_empty() -> None:
    with patch("axon.graph.analysis.async_session", return_value=_mock_session_empty()):
        resp = await team_coupling()
    assert resp.total_cross_team_deps == 0
