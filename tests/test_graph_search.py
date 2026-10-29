"""
Tests for search graph operations.
Mocks async_session to avoid requiring Neo4j.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from axon.graph.models import GraphMeta
from axon.graph.search import api_consumers, find_symbol, semantic_search, service_map


def _mock_session_router():
    mock_session = AsyncMock()

    async def mock_run(*args, **kwargs):
        query = args[0] if args else kwargs.get("query", "")
        mock_result = AsyncMock()
        records = []
        if "MATCH (s:Symbol {qualified_name: $qname})" in query:
            records = [
                {
                    "s": {
                        "id": "1",
                        "name": "foo",
                        "qualified_name": "mod.foo",
                        "kind": "FUNCTION",
                    },
                    "f": {"path": "mod.py"},
                    "t": {"label": "//mod:lib"},
                    "team": {"name": "team-a"},
                }
            ]
        elif "CALL db.index.fulltext.queryNodes" in query:
            records = [
                {
                    "node": {
                        "id": "1",
                        "name": "foo",
                        "qualified_name": "mod.foo",
                        "kind": "FUNCTION",
                        "docstring": "hello",
                    },
                    "score": 1.0,
                    "file_path": "mod.py",
                    "bazel_label": "//mod:lib",
                    "team_name": "team-a",
                }
            ]
        elif "MATCH (e:ApiEndpoint {path: $path, http_method: $method})" in query:
            records = [
                {
                    "swagger_path": "swagger.json",
                    "client_label": "//clients:foo",
                    "language": "PYTHON",
                    "consumer_label": "//a:a",
                    "consumer_kind": "PY_LIBRARY",
                    "team_name": "team-b",
                }
            ]
        elif "MATCH (s:SwaggerSchema)<-[:GENERATED_FROM]-(gc:GeneratedClient)" in query:
            records = [
                {
                    "provider": "//services/payment:lib",
                    "provider_kind": "PY_LIBRARY",
                    "consumer": "//services/checkout:lib",
                    "provider_team": "team-a",
                    "consumer_team": "team-b",
                    "swagger_path": "swagger.json",
                    "endpoint_count": 3,
                }
            ]

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
async def test_find_symbol_found() -> None:
    with patch("axon.graph.search.async_session", return_value=_mock_session_router()):
        resp = await find_symbol("mod.foo")
    assert resp.found is True
    assert resp.symbol.name == "foo"


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_find_symbol_not_found() -> None:
    with patch("axon.graph.search.async_session", return_value=_mock_session_empty()):
        resp = await find_symbol("mod.missing")
    assert resp.found is False


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_semantic_search_found() -> None:
    with patch("axon.graph.search.async_session", return_value=_mock_session_router()):
        resp = await semantic_search("foo")
    assert resp.total == 1
    assert resp.hits[0].name == "foo"


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_api_consumers_found() -> None:
    with patch("axon.graph.search.async_session", return_value=_mock_session_router()):
        resp = await api_consumers("/v1/foo", "GET")
    assert resp.total == 1


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_api_consumers_not_found() -> None:
    with patch("axon.graph.search.async_session", return_value=_mock_session_empty()):
        resp = await api_consumers("/v1/missing", "GET")
    assert resp.total == 0


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_service_map_returns_graph() -> None:
    with patch("axon.graph.search.async_session", return_value=_mock_session_router()):
        resp = await service_map()
    assert resp.total_services > 0
    assert resp.api_dependencies[0].from_service == "//services/checkout:lib"
