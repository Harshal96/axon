from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, computed_field

from axon.models.enums import (
    BazelTargetKind,
    GenruleOutputStatus,
    HttpMethod,
    Language,
    SymbolKind,
)


class BaseNode(BaseModel):
    """All nodes inherit from this."""

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ── SourceFile ─────────────────────────────────────────────────────────────────


class SourceFile(BaseNode):
    """
    Represents a single source file in the repository.
    May be a hand-written source or a genrule output (is_generated=True).
    """

    path: str = Field(description="Repo-relative path, e.g. 'services/payment/src/payment.py'")
    language: Language
    sha256: str = Field(description="SHA-256 of file contents, for incremental updates")
    is_generated: bool = False
    generator_target: str | None = Field(
        default=None, description="Bazel label of the genrule that produced this file, if generated"
    )
    line_count: int = 0
    last_modified_commit: str | None = None

    @computed_field
    @property
    def node_id(self) -> str:
        return f"file::{self.path}"


# ── BazelTarget ────────────────────────────────────────────────────────────────


class BazelTarget(BaseNode):
    """
    A Bazel build target. The fundamental unit of compilation and dependency in the monorepo.
    """

    label: str = Field(description="Canonical Bazel label, e.g. '//services/payment:payment_lib'")
    kind: BazelTargetKind
    name: str = Field(description="The target name portion, e.g. 'payment_lib'")
    package: str = Field(description="The package portion, e.g. '//services/payment'")
    build_file_path: str = Field(
        description="Repo-relative path to the BUILD file defining this target"
    )
    visibility: list[str] = Field(
        default_factory=list,
        description="Bazel visibility list, e.g. ['//...'] or ['//services/checkout:__pkg__']",
    )
    tags: list[str] = Field(default_factory=list)
    team_owner: str | None = None
    testonly: bool = False
    is_genrule: bool = False

    @computed_field
    @property
    def node_id(self) -> str:
        return f"target::{self.label}"


# ── Symbol ─────────────────────────────────────────────────────────────────────


class Symbol(BaseNode):
    """
    A code symbol: function, class, method, variable, etc.
    Extracted by the tree-sitter workers.
    """

    symbol_id: str = Field(description="Stable identifier: 'file_path::ClassName.method_name'")
    name: str = Field(description="Short name, e.g. 'charge'")
    qualified_name: str = Field(
        description="Language-qualified name, e.g. 'payment.PaymentService.charge'"
    )
    kind: SymbolKind
    language: Language
    file_path: str
    line_start: int
    line_end: int
    docstring: str | None = None
    signature: str | None = Field(
        default=None,
        description="Full signature string, e.g. '(amount: Decimal, currency: str) -> ChargeResult'",
    )
    is_public: bool = True
    cyclomatic_complexity: int | None = None
    is_entry_point: bool = False
    is_test: bool = False
    parent_class: str | None = Field(
        default=None, description="Qualified name of the enclosing class, for methods"
    )

    @computed_field
    @property
    def node_id(self) -> str:
        return f"symbol::{self.symbol_id}"


# ── ApiEndpoint ────────────────────────────────────────────────────────────────


class ApiEndpoint(BaseNode):
    """
    An HTTP API endpoint exposed by a service, sourced from a swagger/openapi spec.
    """

    endpoint_id: str = Field(
        description="Stable ID: 'service_label::METHOD:/path', e.g. '//services/payment::POST:/v1/charges'"
    )
    http_method: HttpMethod
    path: str = Field(description="URL path template, e.g. '/v1/charges/{id}'")
    service_label: str = Field(description="Bazel label of the owning service")
    operation_id: str | None = None
    summary: str | None = None
    swagger_source: str = Field(description="Repo-relative path to the swagger.json/openapi.yaml")
    schema_hash: str = Field(
        description="SHA-256 of the combined request+response schema for this endpoint"
    )
    is_deprecated: bool = False
    introduced_at_commit: str | None = None
    last_schema_change_commit: str | None = None

    @computed_field
    @property
    def node_id(self) -> str:
        return f"endpoint::{self.endpoint_id}"


# ── SwaggerSchema ──────────────────────────────────────────────────────────────


class SwaggerSchema(BaseNode):
    """
    A swagger.json or openapi.yaml file. The authoritative API contract for a service.
    A change to this node cascades to GeneratedClient and all consumer BazelTargets.
    """

    path: str = Field(description="Repo-relative path, e.g. 'services/payment/swagger.json'")
    service_label: str = Field(description="Bazel label of the service that owns this spec")
    openapi_version: str = Field(description="e.g. '3.0.1' or '2.0'")
    content_hash: str
    endpoint_count: int = 0
    last_breaking_change_commit: str | None = None
    has_breaking_change: bool = False

    @computed_field
    @property
    def node_id(self) -> str:
        return f"swagger::{self.path}"


# ── GeneratedClient ────────────────────────────────────────────────────────────


class GeneratedClient(BaseNode):
    """
    A Bazel genrule that generates an API client from a SwaggerSchema.
    The critical link between an API contract and the code that consumes it.
    """

    bazel_label: str = Field(
        description="Label of the genrule, e.g. '//clients/payment:payment_client_py'"
    )
    source_swagger: str = Field(description="Repo-relative path to the source swagger.json")
    target_language: Language
    generator_tool: str = Field(description="e.g. 'openapi-generator', 'swagger-codegen'")
    generated_at_commit: str | None = None
    output_paths: list[str] = Field(
        default_factory=list, description="Repo-relative paths of generated output files"
    )
    needs_regeneration: bool = False
    cache_status: GenruleOutputStatus = GenruleOutputStatus.UNKNOWN

    @computed_field
    @property
    def node_id(self) -> str:
        return f"genclient::{self.bazel_label}"


# ── MavenArtifact ──────────────────────────────────────────────────────────────


class MavenArtifact(BaseNode):
    """A Java Maven dependency, discovered from a pom.xml or Maven lockfile."""

    group_id: str
    artifact_id: str
    version: str
    scope: str = "COMPILE"
    is_vulnerable: bool = False
    cve_ids: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def node_id(self) -> str:
        return f"maven::{self.group_id}:{self.artifact_id}:{self.version}"


# ── PnpmPackage ────────────────────────────────────────────────────────────────


class PnpmPackage(BaseNode):
    """A JavaScript/TypeScript package in a pnpm workspace."""

    name: str = Field(description="e.g. '@company/ui-components' or 'lodash'")
    version: str
    workspace_path: str | None = Field(
        default=None,
        description="Repo-relative path for internal workspace: packages, e.g. 'packages/ui-components'",
    )
    is_internal: bool = Field(
        description="True if declared with workspace: protocol in pnpm-lock.yaml"
    )

    @computed_field
    @property
    def node_id(self) -> str:
        return f"pnpm::{self.name}@{self.version}"


# ── Team ───────────────────────────────────────────────────────────────────────


class Team(BaseNode):
    """An engineering team, derived from CODEOWNERS and/or custom Bazel attributes."""

    name: str
    codeowners_patterns: list[str] = Field(
        default_factory=list,
        description="Glob patterns from CODEOWNERS, e.g. ['services/payment/**']",
    )
    slack_channel: str | None = None
    oncall_rotation: str | None = None

    @computed_field
    @property
    def node_id(self) -> str:
        return f"team::{self.name}"


# ── CommitSnapshot ─────────────────────────────────────────────────────────────


class CommitSnapshot(BaseNode):
    """
    Records metadata about a git commit and its indexing status.
    Created after each successful ingestion run.
    """

    sha: str
    author: str | None = None
    commit_timestamp: datetime | None = None
    branch: str | None = None
    graph_version_id: str = Field(
        description="Internal reference to the state of the graph after this commit was indexed"
    )
    changed_targets: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    index_completed_at: datetime | None = None
    index_duration_ms: int | None = None
    index_success: bool = False
    breaking_api_changes: int = 0

    @computed_field
    @property
    def node_id(self) -> str:
        return f"commit::{self.sha}"
