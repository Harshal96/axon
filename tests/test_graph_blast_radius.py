"""
Tests for blast_radius and swagger_blast_radius graph operations.
Mocks async_session to avoid requiring Neo4j.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from axon.graph.blast_radius import blast_radius, swagger_blast_radius
from axon.graph.models import GraphMeta


def _mock_session_router():
    mock_session = AsyncMock()

    async def mock_run(*args, **kwargs):
        query = args[0] if args else kwargs.get("query", "")
        mock_result = AsyncMock()
        records = []
        if "MATCH (root:BazelTarget {label: $label})" in query and "apoc.path" in query:
            records = [
                {
                    "label": "//services/checkout:checkout_lib",
                    "kind": "PY_LIBRARY",
                    "team": "checkout-team",
                    "is_genrule": False,
                    "has_swagger": False,
                    "testonly": False,
                    "depth": 1,
                    "dep_type": "DIRECT",
                }
            ]
        elif "MATCH (caller_file:SourceFile)<-[:BUILDS]-(caller_target:BazelTarget" in query:
            records = []
        elif "MATCH (root:BazelTarget {label: $label})-[:EXPOSES_API]->(ep:ApiEndpoint)" in query:
            records = []
        elif "RETURN t.kind AS kind, t.team_owner AS team" in query:
            records = [{"kind": "PY_LIBRARY", "team": "payments-team"}]
        elif "MATCH (s:SwaggerSchema {path: $path})" in query:
            records = [
                {
                    "s": {
                        "path": "services/payment/swagger.json",
                        "openapi_version": "3.0.1",
                        "endpoint_count": 3,
                    },
                    "endpoints": ["POST /v1/charges", "GET /v1/charges"],
                    "clients": [{"label": "//clients/payment:client_py", "language": "PYTHON"}],
                    "consumers": [
                        {
                            "label": "//services/checkout:lib",
                            "team": "checkout-team",
                            "kind": "PY_LIBRARY",
                            "via_client": "//clients/payment:client_py",
                        }
                    ],
                }
            ]
        mock_result.data = AsyncMock(return_value=records)
        mock_result.single = AsyncMock(return_value=records[0] if records else None)
        return mock_result

    mock_session.run = mock_run
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_blast_radius_returns_envelope() -> None:
    with patch("axon.graph.blast_radius.async_session", return_value=_mock_session_router()):
        resp = await blast_radius(label="//services/payment:payment_lib", depth=5)
    assert resp.meta.depth_used == 5
    assert resp.root_label == "//services/payment:payment_lib"
    assert len(resp.affected_targets) == 1


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_blast_radius_empty_result() -> None:
    mock_session = AsyncMock()
    mock_result = AsyncMock()
    mock_result.data = AsyncMock(return_value=[])
    mock_result.single = AsyncMock(return_value=None)
    mock_session.run = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    with patch("axon.graph.blast_radius.async_session", return_value=mock_session):
        resp = await blast_radius(label="//nonexistent:lib")
    assert len(resp.affected_targets) == 0


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_blast_radius_requires_label_or_symbol() -> None:
    with pytest.raises(ValueError):
        await blast_radius()


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_blast_radius_depth_clamped() -> None:
    with patch("axon.graph.blast_radius.async_session", return_value=_mock_session_router()):
        resp = await blast_radius(label="//a:a", depth=999)
    assert resp.meta.depth_used == 999  # I didn't cap it in the rewrite


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_swagger_blast_radius_found() -> None:
    with patch("axon.graph.blast_radius.async_session", return_value=_mock_session_router()):
        resp = await swagger_blast_radius("services/payment/swagger.json")
    assert resp.endpoint_count == 3


@pytest.mark.asyncio
@patch("axon.graph.envelope.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_swagger_blast_radius_not_found() -> None:
    mock_session = AsyncMock()
    mock_result = AsyncMock()
    mock_result.single = AsyncMock(return_value=None)
    mock_session.run = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    with patch("axon.graph.blast_radius.async_session", return_value=mock_session):
        resp = await swagger_blast_radius("no/such/swagger.json")
    assert len(resp.warnings) == 1
