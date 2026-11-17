"""Caller-owned linking inputs, independent of the graph container."""

from dataclasses import dataclass, field
from typing import Literal

from axon.v1.facts import HttpFact
from axon.v1.models import GraphEdge, GraphNode
from axon.v1.servicemap import ServiceMap


@dataclass
class PreparedCallInput:
    caller_id: str
    file: str
    owner_kind: str
    owner_id: str
    argument_count: int
    static_skip_reason: str
    fact: HttpFact


@dataclass
class PinnedCallInput:
    edge: GraphEdge
    expected_target_id: str
    target_name: str = ""
    target_repo: str = ""
    target_service: str = ""
    target_method: str = ""
    target_path: str = ""
    external_target: GraphNode | None = None


@dataclass
class RepoLinkInputs:
    repo_id: str
    service_map: ServiceMap
    format_version: int = 1
    source_generation: str = ""
    source_commit: str = ""
    manual_sources: dict[str, str] = field(default_factory=dict)
    prepared: list[PreparedCallInput] = field(default_factory=list)
    pinned: list[PinnedCallInput] = field(default_factory=list)
    static_suppression_counts: dict[str, int] = field(default_factory=dict)
    # Legacy absence is incomplete even when no historical edge could be pinned.
    origin: Literal["prepared", "legacy"] = "prepared"
    complete: bool = True
    # None means an older producer has not yet captured source ownership.
    structural_diagnostics: list[str] | None = None


@dataclass
class RepoLinkReport:
    unresolved: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    coverage: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass
class PublicationStats:
    """Measurements owned by one returned publication; cold loads default to zero."""

    replayed_repositories: int = 0
    replayed_sites: int = 0
    structural_nodes: int = 0
    structural_edges: int = 0
    link_nodes: int = 0
    link_edges: int = 0
    input_rows: int = 0
    report_rows: int = 0
    staged_rows: int = 0
    staging_batches: int = 0
    reconcile_s: float = 0.0
    staging_s: float = 0.0
    commit_s: float = 0.0
    cleanup_s: float = 0.0
    total_s: float = 0.0
