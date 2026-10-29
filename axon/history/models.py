"""
Pydantic models for the historical data layer.

PropertyVersion: one node per (node_id, property_name, commit_sha) triple
  where the property value changed. Forms a linked list per property.

CommitDiff: the computed diff between two CommitSnapshot nodes.
  Expensive to compute; cached in Redis after first computation.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field


# ── PropertyVersion (stored in Neo4j) ─────────────────────────────────────────

class PropertyVersionRecord(BaseModel):
    """
    Represents one entry in a property's version history.
    Written to Neo4j as a PropertyVersion node.
    """
    version_id: str           # stable: "propver::{node_id}::{prop_name}::{sha}"
    node_id: str              # the node whose property changed
    node_label: str           # 'Symbol' | 'BazelTarget' | 'ApiEndpoint' | 'SwaggerSchema'
    property_name: str        # 'signature' | 'schema_hash' | 'visibility' | 'deps'
    old_value: Optional[Any]  # None for the first version
    new_value: Any
    changed_at_commit: str    # SHA of the commit that introduced this change
    changed_at: datetime
    author: Optional[str]


# ── Edge validity record (written directly as properties on edges) ─────────────

class EdgeValidityUpdate(BaseModel):
    """
    When an edge is removed (a DEPENDS_ON edge no longer exists), record
    the commit where it became invalid. Written as a SET on the edge.
    """
    from_id: str
    to_id: str
    edge_type: str            # 'DEPENDS_ON' | 'IMPORTS' | 'CALLS' | 'CONSUMES_API'
    valid_until_commit: str
    valid_until_at: datetime


# ── Symbol history entry ───────────────────────────────────────────────────────

class SymbolHistoryEntry(BaseModel):
    commit_sha: str
    commit_timestamp: Optional[datetime]
    author: Optional[str]
    property_name: str
    old_value: Optional[str]
    new_value: str
    is_breaking: bool = False   # True if signature changed in a breaking way


class SymbolHistoryResult(BaseModel):
    symbol_id: str
    symbol_name: str
    current_signature: Optional[str]
    current_file: str
    introduced_at_commit: Optional[str]
    history: list[SymbolHistoryEntry]
    total_changes: int


# ── API endpoint history entry ─────────────────────────────────────────────────

class ApiHistoryEntry(BaseModel):
    commit_sha: str
    commit_timestamp: Optional[datetime]
    author: Optional[str]
    schema_hash_old: Optional[str]
    schema_hash_new: str
    is_breaking: bool
    breaking_changes: list[dict] = Field(default_factory=list)
    # Each entry: {type, field_path, severity, description}


class ApiEndpointHistoryResult(BaseModel):
    endpoint_id: str
    http_method: str
    path: str
    service_label: str
    introduced_at_commit: Optional[str]
    history: list[ApiHistoryEntry]
    total_breaking_changes: int


# ── Graph diff models ──────────────────────────────────────────────────────────

class AddedTarget(BaseModel):
    label: str
    kind: str
    team: Optional[str]
    has_swagger_src: bool
    is_genrule: bool


class RemovedTarget(BaseModel):
    label: str
    kind: str
    team: Optional[str]
    last_seen_commit: str


class ChangedSymbol(BaseModel):
    symbol_id: str
    name: str
    file_path: str
    bazel_label: Optional[str]
    property_changed: str       # 'signature' | 'docstring' | 'kind' | 'file_path'
    old_value: Optional[str]
    new_value: str
    is_breaking: bool


class AddedEdge(BaseModel):
    from_label: str
    to_label: str
    edge_type: str
    from_commit: str


class RemovedEdge(BaseModel):
    from_label: str
    to_label: str
    edge_type: str
    until_commit: str


class ApiSchemaChange(BaseModel):
    endpoint_id: str
    http_method: str
    path: str
    change_type: str         # ADDED | REMOVED | SCHEMA_CHANGED | DEPRECATED
    is_breaking: bool
    details: Optional[str]


class GraphDiffResult(BaseModel):
    sha1: str
    sha2: str
    sha1_timestamp: Optional[datetime]
    sha2_timestamp: Optional[datetime]
    added_targets: list[AddedTarget]
    removed_targets: list[RemovedTarget]
    changed_symbols: list[ChangedSymbol]
    added_edges: list[AddedEdge]
    removed_edges: list[RemovedEdge]
    api_schema_changes: list[ApiSchemaChange]
    summary: dict[str, int]   # counts of each change type


# ── Time-travel result ─────────────────────────────────────────────────────────

class TimeTravelMeta(BaseModel):
    at_commit: str
    at_commit_timestamp: Optional[datetime]
    reconstructed_from_deltas: int    # number of PropertyVersion nodes consulted
    current_head: str
    commits_behind_head: int


class TimeTravelBlastRadiusResult(BaseModel):
    """Same shape as BlastRadiusResult but with time-travel metadata."""
    meta: dict                 # standard GraphMeta + TimeTravelMeta
    root_label: str
    summary: dict[str, Any]
    affected_targets: list[dict]
    affected_by_team: dict[str, int]
    cross_service_api_risks: list[dict]
    warnings: list[dict]
    time_travel: TimeTravelMeta