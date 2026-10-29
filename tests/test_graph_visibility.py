"""
Tests for visibility check graph operation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from axon.graph.models import GraphMeta
from axon.graph.visibility import visibility_check


def _mock_session_router():
    mock_session = AsyncMock()

    async def mock_run(*args, **kwargs):
        query = args[0] if args else kwargs.get("query", "")
        mock_result = AsyncMock()
        records = []
        if kwargs.get("to_label") == "//pkg:public":
            records = [{"visibility": ["//visibility:public"], "package": "//pkg"}]
        elif kwargs.get("to_label") == "//pkg:private":
            records = [{"visibility": ["//visibility:private"], "package": "//pkg"}]
        elif kwargs.get("to_label") == "//pkg:subpkg":
            records = [{"visibility": ["//pkg:__subpackages__"], "package": "//pkg"}]
        elif kwargs.get("to_label") == "//pkg:specific":
            records = [{"visibility": ["//allowed:__pkg__"], "package": "//pkg"}]

        mock_result.data = AsyncMock(return_value=records)
        mock_result.single = AsyncMock(return_value=records[0] if records else None)
        return mock_result

    mock_session.run = mock_run
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.asyncio
@patch("axon.graph.visibility.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_visibility_public() -> None:
    with patch("axon.graph.visibility.async_session", return_value=_mock_session_router()):
        resp = await visibility_check(from_label="//any:thing", to_label="//pkg:public")
    assert resp.is_allowed is True


@pytest.mark.asyncio
@patch("axon.graph.visibility.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_visibility_private_allowed() -> None:
    with patch("axon.graph.visibility.async_session", return_value=_mock_session_router()):
        resp = await visibility_check(from_label="//pkg:thing", to_label="//pkg:private")
    assert resp.is_allowed is True


@pytest.mark.asyncio
@patch("axon.graph.visibility.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_visibility_private_denied() -> None:
    with patch("axon.graph.visibility.async_session", return_value=_mock_session_router()):
        resp = await visibility_check(from_label="//other:thing", to_label="//pkg:private")
    assert resp.is_allowed is False


@pytest.mark.asyncio
@patch("axon.graph.visibility.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_visibility_subpackages() -> None:
    with patch("axon.graph.visibility.async_session", return_value=_mock_session_router()):
        resp = await visibility_check(from_label="//pkg/sub:thing", to_label="//pkg:subpkg")
    assert resp.is_allowed is True


@pytest.mark.asyncio
@patch("axon.graph.visibility.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_visibility_specific_pkg_allowed() -> None:
    with patch("axon.graph.visibility.async_session", return_value=_mock_session_router()):
        resp = await visibility_check(from_label="//allowed:thing", to_label="//pkg:specific")
    assert resp.is_allowed is True
