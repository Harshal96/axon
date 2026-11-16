"""Caller-owned linking inputs, independent of the graph container."""

from dataclasses import dataclass, field

from axon.v1.facts import HttpFact
from axon.v1.models import GraphEdge
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


@dataclass
class RepoLinkReport:
    unresolved: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    coverage: dict[str, dict[str, int]] = field(default_factory=dict)
