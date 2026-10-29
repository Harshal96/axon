"""
Typed Pydantic response models for all graph operations.
Replaces the previous untyped dict-based responses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ── Shared envelope ────────────────────────────────────────────────────────────


class GraphMeta(BaseModel):
    """Included in every operation response."""

    graph_version: str | None = None
    graph_indexed_at: datetime | None = None
    head_commit: str | None = None
    is_stale: bool = False
    stale_by_seconds: int | None = None
    stale_by_commits: int = 0
    query_duration_ms: int = 0
    cache_hit: bool = False
    nodes_traversed: int = 0
    depth_used: int = 0


class Warning(BaseModel):
    code: str
    message: str
    affected_nodes: list[str] = Field(default_factory=list)


# ── blast_radius ───────────────────────────────────────────────────────────────


class AffectedSymbol(BaseModel):
    symbol_id: str
    name: str
    kind: str
    file_path: str
    line: int
    call_chain: list[str] = Field(default_factory=list)


class AffectedTarget(BaseModel):
    label: str
    kind: str
    team: str | None = None
    depth: int
    dependency_type: str  # DIRECT | TRANSITIVE
    impact_confidence: str  # EXTRACTED | INFERRED | AMBIGUOUS
    impact_score: float
    reason: str
    is_generated: bool = False
    affected_symbols: list[AffectedSymbol] = Field(default_factory=list)
    warning: str | None = None


class CrossServiceApiRisk(BaseModel):
    endpoint: str
    consumers: list[str]
    risk_level: str  # HIGH | MEDIUM | LOW
    reason: str


class BlastRadiusResult(BaseModel):
    meta: GraphMeta
    root_label: str
    root_kind: str
    root_team: str | None = None
    summary: dict[str, Any]
    affected_targets: list[AffectedTarget]
    affected_by_team: dict[str, int]
    cross_service_api_risks: list[CrossServiceApiRisk] = Field(default_factory=list)
    warnings: list[Warning] = Field(default_factory=list)


# ── swagger_blast_radius ───────────────────────────────────────────────────────


class AffectedClient(BaseModel):
    label: str
    language: str
    needs_regeneration: bool
    last_generated_at_commit: str | None = None


class AffectedConsumer(BaseModel):
    label: str
    team: str | None = None
    consumes_via: str
    kind: str
    risk: str = "MEDIUM"


class SwaggerBlastRadiusResult(BaseModel):
    meta: GraphMeta
    swagger_path: str
    openapi_version: str | None = None
    endpoint_count: int = 0
    exposed_endpoints: list[str] = Field(default_factory=list)
    affected_generated_clients: list[AffectedClient] = Field(default_factory=list)
    affected_consumer_services: list[AffectedConsumer] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    warnings: list[Warning] = Field(default_factory=list)


# ── subgraph ───────────────────────────────────────────────────────────────────


class KeySymbol(BaseModel):
    name: str
    kind: str
    signature: str | None = None
    docstring: str | None = None
    line: int = 0


class SubgraphTarget(BaseModel):
    label: str
    kind: str
    team: str | None = None
    has_swagger: bool = False
    source_files: list[str] = Field(default_factory=list)
    key_symbols: list[KeySymbol] = Field(default_factory=list)


class SubgraphEdge(BaseModel):
    from_label: str
    to_label: str
    edge_type: str
    depth: int = 1


class ApiContract(BaseModel):
    swagger_path: str
    endpoints: list[str]
    schema_hash: str = ""


class GeneratedCodeRef(BaseModel):
    label: str
    generated_from: str
    warning: str


class SubgraphResult(BaseModel):
    meta: GraphMeta
    architecture_summary: str = ""
    scope: dict[str, Any] = Field(default_factory=dict)
    targets: list[SubgraphTarget] = Field(default_factory=list)
    edges: list[SubgraphEdge] = Field(default_factory=list)
    api_contracts: list[ApiContract] = Field(default_factory=list)
    generated_code: list[GeneratedCodeRef] = Field(default_factory=list)
    token_estimate: int = 0
    warnings: list[Warning] = Field(default_factory=list)


# ── dependency_path ────────────────────────────────────────────────────────────


class DependencyPathResult(BaseModel):
    meta: GraphMeta
    found: bool
    from_label: str
    to_label: str
    hops: int = 0
    paths: list[list[str]] = Field(default_factory=list)
    warnings: list[Warning] = Field(default_factory=list)


# ── callers / callees ──────────────────────────────────────────────────────────


class CallEntry(BaseModel):
    id: str | None = None
    name: str | None = None
    qualified_name: str | None = None
    kind: str | None = None
    depth: int = 1
    file_path: str | None = None
    confidence: str = "EXTRACTED"


class CallGraphResult(BaseModel):
    meta: GraphMeta
    symbol_id: str
    depth: int
    direction: str  # CALLERS | CALLEES
    entries: list[CallEntry] = Field(default_factory=list)
    total: int = 0
    warnings: list[Warning] = Field(default_factory=list)


# ── find_symbol / search ───────────────────────────────────────────────────────


class SymbolDetail(BaseModel):
    id: str | None = None
    name: str | None = None
    qualified_name: str | None = None
    kind: str | None = None
    language: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    signature: str | None = None
    docstring: str | None = None
    is_public: bool | None = None
    is_test: bool | None = None


class SearchHit(BaseModel):
    node_type: str  # Symbol | BazelTarget | ApiEndpoint
    node_id: str | None = None
    name: str | None = None
    qualified_name: str | None = None
    kind: str | None = None
    file_path: str | None = None
    bazel_label: str | None = None
    team: str | None = None
    relevance_score: float = 1.0
    snippet: str | None = None


class FindSymbolResult(BaseModel):
    meta: GraphMeta
    found: bool
    qualified_name: str
    symbol: SymbolDetail | None = None
    file: dict[str, Any] | None = None
    target: dict[str, Any] | None = None
    team: str | None = None
    warnings: list[Warning] = Field(default_factory=list)


class SearchResult(BaseModel):
    meta: GraphMeta
    query: str
    total: int
    hits: list[SearchHit]
    warnings: list[Warning] = Field(default_factory=list)


# ── api_consumers ──────────────────────────────────────────────────────────────


class ApiConsumerEntry(BaseModel):
    label: str
    kind: str | None = None
    team: str | None = None
    via_client: str | None = None
    language: str | None = None


class ApiConsumersResult(BaseModel):
    meta: GraphMeta
    endpoint_path: str
    method: str
    swagger_path: str | None = None
    consumers: list[ApiConsumerEntry] = Field(default_factory=list)
    total: int = 0
    warnings: list[Warning] = Field(default_factory=list)


# ── api_contracts (full view) ──────────────────────────────────────────────────


class EndpointSummary(BaseModel):
    endpoint_id: str | None = None
    method: str
    path: str
    operation_id: str | None = None
    is_deprecated: bool = False
    schema_hash: str = ""


class ApiContractsResult(BaseModel):
    meta: GraphMeta
    service_label: str
    exposes: list[EndpointSummary] = Field(default_factory=list)
    consumes: list[EndpointSummary] = Field(default_factory=list)
    generated_clients: list[GeneratedCodeRef] = Field(default_factory=list)
    consumers_of_my_api: list[ApiConsumerEntry] = Field(default_factory=list)
    warnings: list[Warning] = Field(default_factory=list)


# ── service_map ────────────────────────────────────────────────────────────────


class ServiceNode(BaseModel):
    label: str
    team: str | None = None
    language: str = "UNKNOWN"
    api_endpoint_count: int = 0
    has_swagger: bool = False


class ServiceEdge(BaseModel):
    from_service: str
    to_service: str
    swagger_path: str | None = None
    endpoint_count: int = 0
    via_generated_client: bool = False


class ServiceMapResult(BaseModel):
    meta: GraphMeta
    services: list[ServiceNode] = Field(default_factory=list)
    api_dependencies: list[ServiceEdge] = Field(default_factory=list)
    total_services: int = 0
    total_api_links: int = 0
    warnings: list[Warning] = Field(default_factory=list)


# ── affected_tests ─────────────────────────────────────────────────────────────


class TestTarget(BaseModel):
    label: str
    kind: str | None = None
    team: str | None = None
    reached_via: str | None = None


class AffectedTestsResult(BaseModel):
    meta: GraphMeta
    changed_files: list[str]
    affected_test_targets: list[TestTarget] = Field(default_factory=list)
    total: int = 0
    by_team: dict[str, int] = Field(default_factory=dict)
    warnings: list[Warning] = Field(default_factory=list)

    @property
    def test_targets(self) -> list[TestTarget]:
        """Backward-compatible alias used by the integration test design."""
        return self.affected_test_targets


# ── dead_code ──────────────────────────────────────────────────────────────────


class DeadSymbol(BaseModel):
    name: str
    qualified_name: str | None = None
    kind: str
    file_path: str
    line: int | None = None


class DeadCodeResult(BaseModel):
    meta: GraphMeta
    target: str
    language: str | None = None
    potentially_dead_symbols: list[DeadSymbol] = Field(default_factory=list)
    total: int = 0
    warnings: list[Warning] = Field(default_factory=list)


# ── hub_nodes ──────────────────────────────────────────────────────────────────


class HubNode(BaseModel):
    name: str
    qualified_name: str | None = None
    kind: str | None = None
    call_count: int
    file_path: str | None = None
    target_label: str | None = None


class HubNodesResult(BaseModel):
    meta: GraphMeta
    package_filter: str | None = None
    top_n: int
    hub_nodes: list[HubNode] = Field(default_factory=list)
    warnings: list[Warning] = Field(default_factory=list)


# ── team_coupling ──────────────────────────────────────────────────────────────


class CouplingEntry(BaseModel):
    from_team: str
    to_team: str
    dep_count: int


class TeamCouplingResult(BaseModel):
    meta: GraphMeta
    coupling_matrix: list[CouplingEntry] = Field(default_factory=list)
    total_cross_team_deps: int = 0
    warnings: list[Warning] = Field(default_factory=list)


# ── visibility_check ───────────────────────────────────────────────────────────


class VisibilityCheckResult(BaseModel):
    meta: GraphMeta
    from_label: str
    to_label: str
    is_allowed: bool
    reason: str
    to_visibility: list[str] = Field(default_factory=list)
    suggestion: str | None = None
    warnings: list[Warning] = Field(default_factory=list)
