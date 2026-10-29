"""
HTTP-level tests for all /api/v1/ops/* endpoints.
Uses FastAPI TestClient with mocked graph operation functions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from axon.api.app import create_app
from axon.graph.models import (
    AffectedTestsResult,
    ApiConsumersResult,
    ApiContractsResult,
    BlastRadiusResult,
    CallGraphResult,
    DeadCodeResult,
    DependencyPathResult,
    FindSymbolResult,
    GraphMeta,
    HubNodesResult,
    SearchResult,
    ServiceMapResult,
    SubgraphResult,
    SwaggerBlastRadiusResult,
    TeamCouplingResult,
    VisibilityCheckResult,
)

_META = GraphMeta(query_duration_ms=5, nodes_traversed=3, depth_used=2)


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── blast-radius ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_blast_radius_ok(client) -> None:
    mock_resp = BlastRadiusResult(
        meta=_META,
        root_label="//a:a",
        root_kind="PY_LIBRARY",
        summary={},
        affected_targets=[],
        affected_by_team={},
    )
    with patch("axon.api.routes.ops.br.blast_radius", new=AsyncMock(return_value=mock_resp)):
        resp = await client.get("/api/v1/ops/blast-radius?label=//a:a")
    assert resp.status_code == 200
    assert resp.json()["root_label"] == "//a:a"
    assert "meta" in resp.json()


# ── swagger-blast-radius ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_swagger_blast_radius_ok(client) -> None:
    mock_resp = SwaggerBlastRadiusResult(meta=_META, swagger_path="svc/swagger.json")
    with patch(
        "axon.api.routes.ops.br.swagger_blast_radius", new=AsyncMock(return_value=mock_resp)
    ):
        resp = await client.get("/api/v1/ops/swagger-blast-radius?swagger_path=svc/swagger.json")
    assert resp.status_code == 200


# ── dependency-path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dependency_path_ok(client) -> None:
    mock_resp = DependencyPathResult(meta=_META, found=True, from_label="//a:a", to_label="//b:b")
    with patch("axon.api.routes.ops.tr.dependency_path", new=AsyncMock(return_value=mock_resp)):
        resp = await client.get("/api/v1/ops/dependency-path?from=//a:a&to=//b:b")
    assert resp.status_code == 200


# ── callers / callees ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_callers_ok(client) -> None:
    mock_resp = CallGraphResult(meta=_META, symbol_id="f.py::bar", depth=1, direction="CALLERS")
    with patch("axon.api.routes.ops.tr.callers", new=AsyncMock(return_value=mock_resp)):
        resp = await client.get("/api/v1/ops/callers?symbol_id=f.py::bar")
    assert resp.status_code == 200


# ── subgraph ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subgraph_ok(client) -> None:
    mock_resp = SubgraphResult(meta=_META)
    with patch("axon.api.routes.ops.tr.subgraph", new=AsyncMock(return_value=mock_resp)):
        resp = await client.post("/api/v1/ops/subgraph", json={"labels": ["//a:a"], "depth": 2})
    assert resp.status_code == 200


# ── affected-tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_affected_tests_ok(client) -> None:
    mock_resp = AffectedTestsResult(meta=_META, changed_files=["a/b.py"])
    with patch("axon.api.routes.ops.tr.affected_tests", new=AsyncMock(return_value=mock_resp)):
        resp = await client.post("/api/v1/ops/affected-tests", json={"changed": ["a/b.py"]})
    assert resp.status_code == 200


# ── search ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_symbol_ok(client) -> None:
    mock_resp = FindSymbolResult(meta=_META, found=True, qualified_name="foo")
    with patch("axon.api.routes.ops.se.find_symbol", new=AsyncMock(return_value=mock_resp)):
        resp = await client.get("/api/v1/ops/find-symbol?qualified_name=foo")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_search_ok(client) -> None:
    mock_resp = SearchResult(meta=_META, query="foo", total=0, hits=[])
    with patch("axon.api.routes.ops.se.semantic_search", new=AsyncMock(return_value=mock_resp)):
        resp = await client.get("/api/v1/ops/search?q=foo")
    assert resp.status_code == 200


# ── api-consumers ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_consumers_ok(client) -> None:
    mock_resp = ApiConsumersResult(meta=_META, endpoint_path="/v1", method="POST")
    with patch("axon.api.routes.ops.se.api_consumers", new=AsyncMock(return_value=mock_resp)):
        resp = await client.get("/api/v1/ops/api-consumers?endpoint_path=/v1/charges&method=POST")
    assert resp.status_code == 200


# ── service-map ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_service_map_ok(client) -> None:
    mock_resp = ServiceMapResult(meta=_META)
    with patch("axon.api.routes.ops.se.service_map", new=AsyncMock(return_value=mock_resp)):
        resp = await client.get("/api/v1/ops/service-map")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_api_contracts_ok(client) -> None:
    mock_resp = ApiContractsResult(meta=_META, service_label="//a")
    with patch("axon.api.routes.ops.ac.api_contracts", new=AsyncMock(return_value=mock_resp)):
        resp = await client.get("/api/v1/ops/api-contracts?service_label=//a")
    assert resp.status_code == 200


# ── dead-code ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dead_code_ok(client) -> None:
    mock_resp = DeadCodeResult(meta=_META, target="//a")
    with patch("axon.api.routes.ops.an.dead_code", new=AsyncMock(return_value=mock_resp)):
        resp = await client.get("/api/v1/ops/dead-code?target=//a")
    assert resp.status_code == 200


# ── hub-nodes ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hub_nodes_ok(client) -> None:
    mock_resp = HubNodesResult(meta=_META, top_n=10)
    with patch("axon.api.routes.ops.an.hub_nodes", new=AsyncMock(return_value=mock_resp)):
        resp = await client.get("/api/v1/ops/hub-nodes?top_n=10")
    assert resp.status_code == 200


# ── team-coupling ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_team_coupling_ok(client) -> None:
    mock_resp = TeamCouplingResult(meta=_META)
    with patch("axon.api.routes.ops.an.team_coupling", new=AsyncMock(return_value=mock_resp)):
        resp = await client.get("/api/v1/ops/team-coupling")
    assert resp.status_code == 200


# ── visibility ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_visibility_ok(client) -> None:
    mock_resp = VisibilityCheckResult(
        meta=_META, from_label="//a", to_label="//b", is_allowed=True, reason="ok"
    )
    with patch("axon.api.routes.ops.vi.visibility_check", new=AsyncMock(return_value=mock_resp)):
        resp = await client.get("/api/v1/ops/visibility-check?from_label=//a&to_label=//b")
    assert resp.status_code == 200
