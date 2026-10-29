"""
REST router for all graph query operations.
All endpoints return typed Pydantic models.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel

from axon.api.cache import cache_get, cache_set
from axon.graph import analysis as an
from axon.graph import api_contracts as ac
from axon.graph import blast_radius as br
from axon.graph import search as se
from axon.graph import traversal as tr
from axon.graph import visibility as vi
from axon.graph.models import (
    AffectedTestsResult,
    ApiConsumersResult,
    ApiContractsResult,
    BlastRadiusResult,
    CallGraphResult,
    DeadCodeResult,
    DependencyPathResult,
    FindSymbolResult,
    HubNodesResult,
    SearchResult,
    ServiceMapResult,
    SubgraphResult,
    SwaggerBlastRadiusResult,
    TeamCouplingResult,
    VisibilityCheckResult,
)
from axon.history.models import (
    ApiEndpointHistoryResult,
    GraphDiffResult,
    SymbolHistoryResult,
    TimeTravelBlastRadiusResult,
)
from axon.ops.graph_diff import graph_diff as _graph_diff
from axon.ops.symbol_history import symbol_history as _symbol_history
from axon.ops.time_travel import time_travel_blast_radius as _tt_blast_radius

router = APIRouter()


# ── Request bodies ─────────────────────────────────────────────────────────────


class SubgraphRequest(BaseModel):
    labels: list[str]
    depth: int = 2
    token_budget: int = 4000


class AffectedTestsRequest(BaseModel):
    changed: list[str]


# ── Blast Radius ───────────────────────────────────────────────────────────────


@router.get("/ops/blast-radius", response_model=BlastRadiusResult)
async def blast_radius_endpoint(
    label: str | None = Query(None, description="BazelTarget label"),
    symbol_id: str | None = Query(None, description="Symbol id"),
    depth: int = Query(5, ge=1, le=10),
    include_tests: bool = Query(True),
) -> BlastRadiusResult:
    if not label and not symbol_id:
        raise HTTPException(status_code=422, detail="Provide label or symbol_id")
    params = {
        "label": label,
        "symbol_id": symbol_id,
        "depth": depth,
        "include_tests": include_tests,
    }
    cached = await cache_get("blast_radius", params)
    if cached:
        result = BlastRadiusResult.model_validate_json(cached)
        result.meta.cache_hit = True
        return result
    result = await br.blast_radius(
        label=label, symbol_id=symbol_id, depth=depth, include_tests=include_tests
    )
    await cache_set("blast_radius", params, result.model_dump_json())
    return result


@router.get("/ops/swagger-blast-radius", response_model=SwaggerBlastRadiusResult)
async def swagger_blast_radius_endpoint(
    swagger_path: str = Query(..., description="Repo-relative path to swagger.json"),
) -> SwaggerBlastRadiusResult:
    result = await br.swagger_blast_radius(swagger_path)
    return result


# ── Traversal ──────────────────────────────────────────────────────────────────


@router.get("/ops/dependency-path", response_model=DependencyPathResult)
async def dependency_path_endpoint(
    from_label: str = Query(..., alias="from"),
    to_label: str = Query(..., alias="to"),
) -> DependencyPathResult:
    result = await tr.dependency_path(from_label, to_label)
    return result


@router.get("/ops/callers", response_model=CallGraphResult)
async def callers_endpoint(
    symbol_id: str = Query(...),
    depth: int = Query(1, ge=1, le=5),
) -> CallGraphResult:
    result = await tr.callers(symbol_id, depth)
    return result


@router.get("/ops/callees", response_model=CallGraphResult)
async def callees_endpoint(
    symbol_id: str = Query(...),
    depth: int = Query(3, ge=1, le=7),
) -> CallGraphResult:
    result = await tr.callees(symbol_id, depth)
    return result


@router.post("/ops/subgraph", response_model=SubgraphResult)
async def subgraph_endpoint(
    body: SubgraphRequest | list[str] = Body(...),
    depth: int = Query(2, ge=1, le=4),
    token_budget: int = Query(4000, ge=500, le=16000),
) -> SubgraphResult:
    if isinstance(body, list):
        result = await tr.subgraph(body, depth, token_budget)
    else:
        result = await tr.subgraph(body.labels, body.depth, body.token_budget)
    return result


@router.post("/ops/affected-tests", response_model=AffectedTestsResult)
async def affected_tests_endpoint(
    body: AffectedTestsRequest | list[str] = Body(...),
) -> AffectedTestsResult:
    changed = body if isinstance(body, list) else body.changed
    result = await tr.affected_tests(changed)
    return result


@router.post("/ops/refresh")
async def refresh_graph_cache() -> dict[str, str]:
    from axon.api.cache import cache_invalidate
    from axon.api.routes.ws import broadcast_graph_updated

    await cache_invalidate()
    await broadcast_graph_updated()
    return {"status": "accepted"}


# ── Search ─────────────────────────────────────────────────────────────────────


@router.get("/ops/find-symbol", response_model=FindSymbolResult)
async def find_symbol_endpoint(
    qualified_name: str = Query(...),
) -> FindSymbolResult:
    result = await se.find_symbol(qualified_name)
    return result


@router.get("/ops/search", response_model=SearchResult)
async def semantic_search_endpoint(
    q: str = Query(..., min_length=2),
    scope: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
) -> SearchResult:
    result = await se.semantic_search(q, scope, limit)
    return result


@router.get("/ops/api-consumers", response_model=ApiConsumersResult)
async def api_consumers_endpoint(
    endpoint_path: str = Query(...),
    method: str = Query(...),
) -> ApiConsumersResult:
    result = await se.api_consumers(endpoint_path, method)
    return result


@router.get("/ops/service-map", response_model=ServiceMapResult)
async def service_map_endpoint() -> ServiceMapResult:
    result = await se.service_map()
    return result


@router.get("/ops/api-contracts", response_model=ApiContractsResult)
async def api_contracts_endpoint(
    service_label: str = Query(...),
) -> ApiContractsResult:
    result = await ac.api_contracts(service_label)
    return result


# ── Analysis ───────────────────────────────────────────────────────────────────


@router.get("/ops/dead-code", response_model=DeadCodeResult)
async def dead_code_endpoint(
    target: str = Query(..., description="BazelTarget label"),
    language: str | None = Query(None),
) -> DeadCodeResult:
    result = await an.dead_code(target, language)
    return result


@router.get("/ops/hub-nodes", response_model=HubNodesResult)
async def hub_nodes_endpoint(
    package: str | None = Query(None),
    top_n: int = Query(20, ge=1, le=100),
) -> HubNodesResult:
    result = await an.hub_nodes(package, top_n)
    return result


@router.get("/ops/team-coupling", response_model=TeamCouplingResult)
async def team_coupling_endpoint() -> TeamCouplingResult:
    result = await an.team_coupling()
    return result


# ── Visibility ─────────────────────────────────────────────────────────────────


@router.get("/ops/visibility-check", response_model=VisibilityCheckResult)
async def visibility_check_endpoint(
    from_label: str = Query(...),
    to_label: str = Query(...),
) -> VisibilityCheckResult:
    result = await vi.visibility_check(from_label, to_label)
    return result


@router.get("/ops/graph-diff", response_model=GraphDiffResult)
async def get_graph_diff(
    sha1: str = Query(..., description="Older commit SHA"),
    sha2: str = Query(..., description="Newer commit SHA"),
) -> GraphDiffResult:
    params = {"sha1": sha1, "sha2": sha2}
    cached = await cache_get("graph_diff", params)
    if cached:
        result = GraphDiffResult.model_validate_json(cached)
        return result
    result = await _graph_diff(sha1, sha2)
    await cache_set("graph_diff", params, result.model_dump_json())
    return result


@router.get("/ops/symbol-history", response_model=SymbolHistoryResult)
async def get_symbol_history(
    symbol_id: str = Query(...),
) -> SymbolHistoryResult:
    params = {"symbol_id": symbol_id}
    cached = await cache_get("symbol_history", params)
    if cached:
        return SymbolHistoryResult.model_validate_json(cached)
    result = await _symbol_history(symbol_id)
    await cache_set("symbol_history", params, result.model_dump_json())
    return result


@router.get("/ops/api-endpoint-history", response_model=ApiEndpointHistoryResult)
async def get_api_endpoint_history(
    endpoint_id: str = Query(...),
) -> ApiEndpointHistoryResult:
    params = {"endpoint_id": endpoint_id}
    cached = await cache_get("api_history", params)
    if cached:
        return ApiEndpointHistoryResult.model_validate_json(cached)
    from axon.ops.api_history import api_endpoint_history
    result = await api_endpoint_history(endpoint_id)
    await cache_set("api_history", params, result.model_dump_json())
    return result


@router.get(
    "/ops/time-travel/blast-radius",
    response_model=TimeTravelBlastRadiusResult,
)
async def time_travel_blast_radius_endpoint(
    label: str = Query(...),
    at_commit: str = Query(...),
    depth: int = Query(5, ge=1, le=10),
) -> TimeTravelBlastRadiusResult:
    params = {"label": label, "at_commit": at_commit, "depth": depth}
    cached = await cache_get("time_travel_blast_radius", params)
    if cached:
        return TimeTravelBlastRadiusResult.model_validate_json(cached)
    result = await _tt_blast_radius(label, at_commit, depth)
    # Time-travel results are immutable (historical) — cache for a long time
    await cache_set("time_travel_blast_radius", params, result.model_dump_json())
    return result
