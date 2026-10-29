from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from axon.models.enums import (
    DependencyType,
    EdgeConfidence,
    ImportType,
    MavenScope,
)


class BaseEdge(BaseModel):
    """All edge models inherit from this. from_id and to_id are node_id values."""

    from_id: str
    to_id: str
    confidence: EdgeConfidence = EdgeConfidence.EXTRACTED
    confidence_score: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # Temporal validity for historical queries
    valid_from_commit: str | None = None
    valid_until_commit: str | None = None  # None = still active


# ── Code structure edges ───────────────────────────────────────────────────────


class CallsEdge(BaseEdge):
    """(Symbol) -[:CALLS]-> (Symbol)"""

    call_sites: list[dict] = Field(
        default_factory=list, description="List of {line: int, file: str} where this call occurs"
    )
    call_count: int = 1
    is_dynamic: bool = Field(
        default=False, description="True for dynamic dispatch, reflection, or __getattr__ patterns"
    )


class DefinesEdge(BaseEdge):
    """(SourceFile) -[:DEFINES]-> (Symbol)"""

    pass


class ImportsEdge(BaseEdge):
    """(SourceFile) -[:IMPORTS]-> (SourceFile)"""

    import_type: ImportType = ImportType.STATIC
    import_names: list[str] = Field(
        default_factory=list,
        description="Specific names imported, e.g. ['PaymentService', 'ChargeResult']",
    )


class ExtendsEdge(BaseEdge):
    """(Symbol class) -[:EXTENDS]-> (Symbol class)"""

    pass


class ImplementsEdge(BaseEdge):
    """(Symbol class) -[:IMPLEMENTS]-> (Symbol interface)"""

    pass


class OverridesEdge(BaseEdge):
    """(Symbol method) -[:OVERRIDES]-> (Symbol method)"""

    pass


# ── Bazel structure edges ──────────────────────────────────────────────────────


class DependsOnEdge(BaseEdge):
    """(BazelTarget) -[:DEPENDS_ON]-> (BazelTarget)"""

    dep_type: DependencyType = DependencyType.DIRECT
    declared_in: str = Field(description="BUILD file path where this dep is declared")


class BuildsEdge(BaseEdge):
    """(BazelTarget) -[:BUILDS]-> (SourceFile)  — target owns/compiles this file"""

    pass


class ProducesEdge(BaseEdge):
    """(BazelTarget genrule) -[:PRODUCES]-> (SourceFile generated output)"""

    pass


class ConsumesFileEdge(BaseEdge):
    """(BazelTarget genrule) -[:CONSUMES_FILE]-> (SourceFile input to genrule srcs)"""

    pass


class OwnedByEdge(BaseEdge):
    """(BazelTarget or SourceFile) -[:OWNED_BY]-> (Team)"""

    ownership_source: str = Field(
        default="CODEOWNERS",
        description="How ownership was determined: CODEOWNERS | BAZEL_ATTR | INFERRED",
    )


# ── API contract edges ─────────────────────────────────────────────────────────


class ExposesApiEdge(BaseEdge):
    """(BazelTarget) -[:EXPOSES_API]-> (ApiEndpoint)"""

    pass


class ConsumesApiEdge(BaseEdge):
    """(BazelTarget consumer) -[:CONSUMES_API]-> (ApiEndpoint)"""

    via_client: str | None = Field(
        default=None,
        description="Bazel label of the GeneratedClient through which this API is consumed",
    )
    client_language: str | None = None


class GeneratesClientFromEdge(BaseEdge):
    """(BazelTarget genrule) -[:GENERATES_CLIENT_FROM]-> (SwaggerSchema)"""

    pass


class GeneratedFromEdge(BaseEdge):
    """(GeneratedClient) -[:GENERATED_FROM]-> (SwaggerSchema)"""

    pass


class SwaggerDefinesEndpointEdge(BaseEdge):
    """(SwaggerSchema) -[:DEFINES_ENDPOINT]-> (ApiEndpoint)"""

    pass


# ── Package manager edges ──────────────────────────────────────────────────────


class MavenDepEdge(BaseEdge):
    """(BazelTarget) -[:MAVEN_DEP]-> (MavenArtifact)"""

    scope: MavenScope = MavenScope.COMPILE
    declared_in: str = Field(description="Path to the pom.xml declaring this dep")


class PnpmDepEdge(BaseEdge):
    """(BazelTarget) -[:PNPM_DEP]-> (PnpmPackage)"""

    declared_in: str = Field(description="Path to the package.json declaring this dep")


# ── Commit edges ───────────────────────────────────────────────────────────────


class IntroducedByEdge(BaseEdge):
    """(Symbol or BazelTarget) -[:INTRODUCED_BY]-> (CommitSnapshot)"""

    author: str | None = None
