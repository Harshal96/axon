"""
Neo4j MERGE-based writers. All writes are idempotent.
Batch size: 500 nodes, 1000 edges per transaction.
Never interpolate user strings into Cypher — use $params always.
"""

from __future__ import annotations

import logging
from typing import Any

from axon.db.connection import sync_session

logger = logging.getLogger(__name__)

_BATCH_SIZE_NODES = 500
_BATCH_SIZE_EDGES = 1000

# ── BazelTarget writer ─────────────────────────────────────────────────────────

_MERGE_BAZEL_TARGET = """
UNWIND $batch AS props
MERGE (t:BazelTarget {id: props.id})
ON CREATE SET
    t.label             = props.label,
    t.kind              = props.kind,
    t.name              = props.name,
    t.package           = props.package,
    t.build_file_path   = props.build_file_path,
    t.visibility        = props.visibility,
    t.tags              = props.tags,
    t.testonly          = props.testonly,
    t.is_genrule        = props.is_genrule,
    t.team_owner        = props.team_owner,
    t.has_swagger_src   = props.has_swagger_src,
    t.has_maven_src     = props.has_maven_src,
    t.has_pnpm_src      = props.has_pnpm_src,
    t.genrule_srcs      = props.genrule_srcs,
    t.genrule_outs      = props.genrule_outs,
    t.created_at        = datetime()
ON MATCH SET
    t.visibility        = props.visibility,
    t.tags              = props.tags,
    t.testonly          = props.testonly,
    t.team_owner        = props.team_owner,
    t.has_swagger_src   = props.has_swagger_src,
    t.has_maven_src     = props.has_maven_src,
    t.has_pnpm_src      = props.has_pnpm_src,
    t.genrule_srcs      = props.genrule_srcs,
    t.genrule_outs      = props.genrule_outs,
    t.updated_at        = datetime()
RETURN count(t) AS written
"""


def write_bazel_targets(targets: list[dict[str, Any]]) -> int:
    """Batch-merges BazelTarget nodes. Returns total written/updated."""
    total = 0
    with sync_session() as session:
        for i in range(0, len(targets), _BATCH_SIZE_NODES):
            batch = targets[i : i + _BATCH_SIZE_NODES]
            result = session.run(_MERGE_BAZEL_TARGET, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── DependsOn edge writer ──────────────────────────────────────────────────────

_MERGE_DEPENDS_ON = """
UNWIND $batch AS props
MATCH (from:BazelTarget {id: props.from_id})
MATCH (to:BazelTarget   {id: props.to_id})
MERGE (from)-[r:DEPENDS_ON]->(to)
SET
    r.dep_type          = props.dep_type,
    r.declared_in       = props.declared_in,
    r.confidence        = props.confidence,
    r.valid_from_commit = props.valid_from_commit,
    r.updated_at        = datetime()
RETURN count(r) AS written
"""


def write_depends_on_edges(edges: list[dict[str, Any]]) -> int:
    """Batch-merges DEPENDS_ON edges. Both endpoints must exist."""
    total = 0
    with sync_session() as session:
        for i in range(0, len(edges), _BATCH_SIZE_EDGES):
            batch = edges[i : i + _BATCH_SIZE_EDGES]
            result = session.run(_MERGE_DEPENDS_ON, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── Team writer ────────────────────────────────────────────────────────────────

_MERGE_TEAM = """
UNWIND $batch AS props
MERGE (t:Team {id: props.id})
ON CREATE SET
    t.name                  = props.name,
    t.codeowners_patterns   = props.codeowners_patterns,
    t.created_at            = datetime()
ON MATCH SET
    t.codeowners_patterns   = props.codeowners_patterns,
    t.updated_at            = datetime()
RETURN count(t) AS written
"""

_MERGE_OWNED_BY = """
UNWIND $batch AS props
MATCH (t:BazelTarget {id: props.target_id})
MATCH (team:Team     {id: props.team_id})
MERGE (t)-[r:OWNED_BY]->(team)
SET r.ownership_source = props.ownership_source,
    r.updated_at       = datetime()
RETURN count(r) AS written
"""


def write_teams(teams: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(teams), _BATCH_SIZE_NODES):
            batch = teams[i : i + _BATCH_SIZE_NODES]
            result = session.run(_MERGE_TEAM, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


def write_owned_by_edges(edges: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(edges), _BATCH_SIZE_EDGES):
            batch = edges[i : i + _BATCH_SIZE_EDGES]
            result = session.run(_MERGE_OWNED_BY, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── SourceFile writer ──────────────────────────────────────────────────────────

_MERGE_SOURCE_FILE = """
UNWIND $batch AS props
MERGE (f:SourceFile {id: props.id})
ON CREATE SET
    f.path          = props.path,
    f.language      = props.language,
    f.sha256        = props.sha256,
    f.line_count    = props.line_count,
    f.is_generated  = props.is_generated,
    f.created_at    = datetime()
ON MATCH SET
    f.sha256        = props.sha256,
    f.line_count    = props.line_count,
    f.is_generated  = props.is_generated,
    f.updated_at    = datetime()
RETURN count(f) AS written
"""


def write_source_files(files: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(files), _BATCH_SIZE_NODES):
            batch = files[i : i + _BATCH_SIZE_NODES]
            result = session.run(_MERGE_SOURCE_FILE, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── Symbol writer ──────────────────────────────────────────────────────────────

_MERGE_SYMBOL = """
UNWIND $batch AS props
MERGE (s:Symbol {id: props.id})
ON CREATE SET
    s.symbol_id      = props.symbol_id,
    s.name           = props.name,
    s.qualified_name = props.qualified_name,
    s.kind           = props.kind,
    s.language       = props.language,
    s.file_path      = props.file_path,
    s.line_start     = props.line_start,
    s.line_end       = props.line_end,
    s.docstring      = props.docstring,
    s.signature      = props.signature,
    s.is_public      = props.is_public,
    s.is_test        = props.is_test,
    s.parent_class   = props.parent_class,
    s.created_at     = datetime()
ON MATCH SET
    s.line_start     = props.line_start,
    s.line_end       = props.line_end,
    s.docstring      = props.docstring,
    s.signature      = props.signature,
    s.is_public      = props.is_public,
    s.is_test        = props.is_test,
    s.updated_at     = datetime()
RETURN count(s) AS written
"""


def write_symbols(symbols: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(symbols), _BATCH_SIZE_NODES):
            batch = symbols[i : i + _BATCH_SIZE_NODES]
            result = session.run(_MERGE_SYMBOL, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── BUILDS edge writer (BazelTarget → SourceFile) ─────────────────────────────

_MERGE_BUILDS = """
UNWIND $batch AS props
MATCH (t:BazelTarget {id: props.from_id})
MATCH (f:SourceFile  {id: props.to_id})
MERGE (t)-[r:BUILDS]->(f)
SET
    r.confidence        = props.confidence,
    r.valid_from_commit = props.valid_from_commit,
    r.updated_at        = datetime()
RETURN count(r) AS written
"""


def write_builds_edges(edges: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(edges), _BATCH_SIZE_EDGES):
            batch = edges[i : i + _BATCH_SIZE_EDGES]
            result = session.run(_MERGE_BUILDS, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── DEFINES edge writer (SourceFile → Symbol) ─────────────────────────────────

_MERGE_DEFINES = """
UNWIND $batch AS props
MATCH (f:SourceFile {id: props.from_id})
MATCH (s:Symbol     {id: props.to_id})
MERGE (f)-[r:DEFINES]->(s)
SET
    r.confidence        = props.confidence,
    r.valid_from_commit = props.valid_from_commit,
    r.updated_at        = datetime()
RETURN count(r) AS written
"""


def write_defines_edges(edges: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(edges), _BATCH_SIZE_EDGES):
            batch = edges[i : i + _BATCH_SIZE_EDGES]
            result = session.run(_MERGE_DEFINES, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── IMPORTS edge writer (SourceFile → SourceFile) ─────────────────────────────

_MERGE_IMPORTS = """
UNWIND $batch AS props
MATCH (from:SourceFile {id: props.from_id})
MATCH (to:SourceFile   {id: props.to_id})
MERGE (from)-[r:IMPORTS]->(to)
SET
    r.import_names      = props.import_names,
    r.confidence        = props.confidence,
    r.valid_from_commit = props.valid_from_commit,
    r.updated_at        = datetime()
RETURN count(r) AS written
"""


def write_imports_edges(edges: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(edges), _BATCH_SIZE_EDGES):
            batch = edges[i : i + _BATCH_SIZE_EDGES]
            result = session.run(_MERGE_IMPORTS, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── Cross-language edge (SourceFile → BazelTarget) ─────────────────────────────

_MERGE_CROSS_LANG = """
UNWIND $batch AS props
MATCH (from {id: props.from_id})
MATCH (to   {id: props.to_id})
MERGE (from)-[r:CROSS_LANG_DEP]->(to)
SET r.confidence        = props.confidence,
    r.confidence_score  = props.confidence_score,
    r.consumer_language = props.consumer_language,
    r.provider_language = props.provider_language,
    r.updated_at        = datetime()
RETURN count(r) AS written
"""


def write_cross_lang_edges(edges: list[dict]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(edges), _BATCH_SIZE_EDGES):
            batch = edges[i : i + _BATCH_SIZE_EDGES]
            result = session.run(_MERGE_CROSS_LANG, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── CALLS edge writer (Symbol → Symbol) ───────────────────────────────────────

_MERGE_CALLS = """
UNWIND $batch AS props
MATCH (from:Symbol {id: props.from_id})
MATCH (to:Symbol   {id: props.to_id})
MERGE (from)-[r:CALLS]->(to)
SET
    r.call_line         = props.call_line,
    r.confidence        = props.confidence,
    r.valid_from_commit = props.valid_from_commit,
    r.updated_at        = datetime()
RETURN count(r) AS written
"""


def write_calls_edges(edges: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(edges), _BATCH_SIZE_EDGES):
            batch = edges[i : i + _BATCH_SIZE_EDGES]
            result = session.run(_MERGE_CALLS, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── SwaggerSchema writer ──────────────────────────────────────────────────────

_MERGE_SWAGGER_SCHEMA = """
UNWIND $batch AS props
MERGE (s:SwaggerSchema {id: props.id})
ON CREATE SET
    s.path              = props.path,
    s.service_label     = props.service_label,
    s.openapi_version   = props.openapi_version,
    s.content_hash      = props.content_hash,
    s.endpoint_count    = props.endpoint_count,
    s.created_at        = datetime()
ON MATCH SET
    s.content_hash      = props.content_hash,
    s.endpoint_count    = props.endpoint_count,
    s.updated_at        = datetime()
RETURN count(s) AS written
"""


def write_swagger_schemas(schemas: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(schemas), _BATCH_SIZE_NODES):
            batch = schemas[i : i + _BATCH_SIZE_NODES]
            result = session.run(_MERGE_SWAGGER_SCHEMA, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── ApiEndpoint writer ─────────────────────────────────────────────────────────

_MERGE_API_ENDPOINT = """
UNWIND $batch AS props
MERGE (e:ApiEndpoint {id: props.id})
ON CREATE SET
    e.endpoint_id    = props.endpoint_id,
    e.http_method    = props.http_method,
    e.path           = props.path,
    e.service_label  = props.service_label,
    e.operation_id   = props.operation_id,
    e.summary        = props.summary,
    e.swagger_source = props.swagger_source,
    e.schema_hash    = props.schema_hash,
    e.is_deprecated  = props.is_deprecated,
    e.created_at     = datetime()
ON MATCH SET
    e.summary        = props.summary,
    e.schema_hash    = props.schema_hash,
    e.is_deprecated  = props.is_deprecated,
    e.updated_at     = datetime()
RETURN count(e) AS written
"""


def write_api_endpoints(endpoints: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(endpoints), _BATCH_SIZE_NODES):
            batch = endpoints[i : i + _BATCH_SIZE_NODES]
            result = session.run(_MERGE_API_ENDPOINT, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── GeneratedClient writer ─────────────────────────────────────────────────────

_MERGE_GENERATED_CLIENT = """
UNWIND $batch AS props
MERGE (g:GeneratedClient {id: props.id})
ON CREATE SET
    g.bazel_label       = props.bazel_label,
    g.source_swagger    = props.source_swagger,
    g.target_language   = props.target_language,
    g.generator_tool    = props.generator_tool,
    g.output_paths      = props.output_paths,
    g.created_at        = datetime()
ON MATCH SET
    g.source_swagger    = props.source_swagger,
    g.target_language   = props.target_language,
    g.output_paths      = props.output_paths,
    g.updated_at        = datetime()
RETURN count(g) AS written
"""


def write_generated_clients(clients: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(clients), _BATCH_SIZE_NODES):
            batch = clients[i : i + _BATCH_SIZE_NODES]
            result = session.run(_MERGE_GENERATED_CLIENT, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── MavenArtifact writer ───────────────────────────────────────────────────────

_MERGE_MAVEN_ARTIFACT = """
UNWIND $batch AS props
MERGE (m:MavenArtifact {id: props.id})
ON CREATE SET
    m.group_id      = props.group_id,
    m.artifact_id   = props.artifact_id,
    m.version       = props.version,
    m.scope         = props.scope,
    m.is_vulnerable = false,
    m.created_at    = datetime()
ON MATCH SET
    m.scope         = props.scope,
    m.updated_at    = datetime()
RETURN count(m) AS written
"""


def write_maven_artifacts(artifacts: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(artifacts), _BATCH_SIZE_NODES):
            batch = artifacts[i : i + _BATCH_SIZE_NODES]
            result = session.run(_MERGE_MAVEN_ARTIFACT, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── PnpmPackage writer ─────────────────────────────────────────────────────────

_MERGE_PNPM_PACKAGE = """
UNWIND $batch AS props
MERGE (p:PnpmPackage {id: props.id})
ON CREATE SET
    p.name           = props.name,
    p.version        = props.version,
    p.is_internal    = props.is_internal,
    p.workspace_path = props.workspace_path,
    p.created_at     = datetime()
ON MATCH SET
    p.is_internal    = props.is_internal,
    p.workspace_path = props.workspace_path,
    p.updated_at     = datetime()
RETURN count(p) AS written
"""


def write_pnpm_packages(packages: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(packages), _BATCH_SIZE_NODES):
            batch = packages[i : i + _BATCH_SIZE_NODES]
            result = session.run(_MERGE_PNPM_PACKAGE, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── DEFINES_ENDPOINT edge (SwaggerSchema → ApiEndpoint) ───────────────────────

_MERGE_DEFINES_ENDPOINT = """
UNWIND $batch AS props
MATCH (s:SwaggerSchema {id: props.from_id})
MATCH (e:ApiEndpoint   {id: props.to_id})
MERGE (s)-[r:DEFINES_ENDPOINT]->(e)
SET
    r.confidence        = props.confidence,
    r.valid_from_commit = props.valid_from_commit,
    r.updated_at        = datetime()
RETURN count(r) AS written
"""


def write_defines_endpoint_edges(edges: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(edges), _BATCH_SIZE_EDGES):
            batch = edges[i : i + _BATCH_SIZE_EDGES]
            result = session.run(_MERGE_DEFINES_ENDPOINT, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── GENERATED_FROM edge (GeneratedClient → SwaggerSchema) ─────────────────────

_MERGE_GENERATED_FROM = """
UNWIND $batch AS props
MATCH (g:GeneratedClient {id: props.from_id})
MATCH (s:SwaggerSchema   {id: props.to_id})
MERGE (g)-[r:GENERATED_FROM]->(s)
SET
    r.confidence        = props.confidence,
    r.valid_from_commit = props.valid_from_commit,
    r.updated_at        = datetime()
RETURN count(r) AS written
"""


def write_generated_from_edges(edges: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(edges), _BATCH_SIZE_EDGES):
            batch = edges[i : i + _BATCH_SIZE_EDGES]
            result = session.run(_MERGE_GENERATED_FROM, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── MAVEN_DEP edge (BazelTarget → MavenArtifact) ──────────────────────────────

_MERGE_MAVEN_DEP = """
UNWIND $batch AS props
MATCH (t:BazelTarget    {id: props.from_id})
MATCH (m:MavenArtifact  {id: props.to_id})
MERGE (t)-[r:MAVEN_DEP]->(m)
SET
    r.scope             = props.scope,
    r.declared_in       = props.declared_in,
    r.confidence        = props.confidence,
    r.valid_from_commit = props.valid_from_commit,
    r.updated_at        = datetime()
RETURN count(r) AS written
"""


def write_maven_dep_edges(edges: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(edges), _BATCH_SIZE_EDGES):
            batch = edges[i : i + _BATCH_SIZE_EDGES]
            result = session.run(_MERGE_MAVEN_DEP, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── PNPM_DEP edge (BazelTarget → PnpmPackage) ─────────────────────────────────

_MERGE_PNPM_DEP = """
UNWIND $batch AS props
MATCH (t:BazelTarget {id: props.from_id})
MATCH (p:PnpmPackage {id: props.to_id})
MERGE (t)-[r:PNPM_DEP]->(p)
SET
    r.declared_in       = props.declared_in,
    r.confidence        = props.confidence,
    r.valid_from_commit = props.valid_from_commit,
    r.updated_at        = datetime()
RETURN count(r) AS written
"""


def write_pnpm_dep_edges(edges: list[dict[str, Any]]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(edges), _BATCH_SIZE_EDGES):
            batch = edges[i : i + _BATCH_SIZE_EDGES]
            result = session.run(_MERGE_PNPM_DEP, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── SourceFile writer ──────────────────────────────────────────────────────────

_MERGE_SOURCE_FILE = """
UNWIND $batch AS props
MERGE (sf:SourceFile {id: props.id})
ON CREATE SET
    sf.path              = props.path,
    sf.language          = props.language,
    sf.sha256            = props.sha256,
    sf.is_generated      = props.is_generated,
    sf.line_count        = props.line_count,
    sf.created_at        = datetime()
ON MATCH SET
    sf.sha256            = props.sha256,
    sf.line_count        = props.line_count,
    sf.updated_at        = datetime()
RETURN count(sf) AS written
"""


def write_source_files(files: list[dict]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(files), _BATCH_SIZE_NODES):
            batch = files[i : i + _BATCH_SIZE_NODES]
            result = session.run(_MERGE_SOURCE_FILE, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── RE_EXPORTS edge ────────────────────────────────────────────────────────────

_MERGE_RE_EXPORTS = """
UNWIND $batch AS props
MATCH (from:SourceFile {id: props.from_id})
MATCH (to:SourceFile   {id: props.to_id})
MERGE (from)-[r:RE_EXPORTS]->(to)
SET r.exported_names = props.exported_names,
    r.updated_at     = datetime()
RETURN count(r) AS written
"""


def write_re_export_edges(edges: list[dict]) -> int:
    """
    Re-export edges require resolving the source_module to a SourceFile id
    before writing. This is done here rather than in the worker to keep the
    worker logic clean.
    """
    import asyncio

    from axon.workers.ts.resolver import resolve_import

    resolved: list[dict] = []
    loop = asyncio.new_event_loop()

    for edge in edges:
        try:
            target_id, _, confidence = loop.run_until_complete(
                resolve_import(
                    edge["source_module"],
                    edge["base_dir"] + "/placeholder.ts",
                    edge["workspace_root"],
                )
            )
        except Exception:
            continue
        if target_id and target_id.startswith("file::"):
            resolved.append(
                {
                    "from_id": edge["from_file_id"],
                    "to_id": target_id,
                    "exported_names": edge["exported_names"],
                }
            )

    loop.close()
    if not resolved:
        return 0

    total = 0
    with sync_session() as session:
        for i in range(0, len(resolved), _BATCH_SIZE_EDGES):
            batch = resolved[i : i + _BATCH_SIZE_EDGES]
            result = session.run(_MERGE_RE_EXPORTS, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── PnpmPackage writer ─────────────────────────────────────────────────────────

_MERGE_PNPM_PACKAGE = """
UNWIND $batch AS props
MERGE (p:PnpmPackage {id: props.id})
ON CREATE SET
    p.name           = props.name,
    p.version        = props.version,
    p.workspace_path = props.workspace_path,
    p.is_internal    = props.is_internal,
    p.bazel_label    = props.bazel_label,
    p.created_at     = datetime()
ON MATCH SET
    p.version        = props.version,
    p.bazel_label    = props.bazel_label,
    p.updated_at     = datetime()
RETURN count(p) AS written
"""


def write_pnpm_packages(packages: list[dict]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(packages), _BATCH_SIZE_NODES):
            batch = packages[i : i + _BATCH_SIZE_NODES]
            result = session.run(_MERGE_PNPM_PACKAGE, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total


# ── PNPM_DEP edge ──────────────────────────────────────────────────────────────

_MERGE_PNPM_DEP = """
UNWIND $batch AS props
MATCH (t {id: props.from_id})
MATCH (p:PnpmPackage {id: props.to_id})
MERGE (t)-[r:PNPM_DEP]->(p)
SET r.declared_in = props.declared_in,
    r.updated_at  = datetime()
RETURN count(r) AS written
"""


def write_pnpm_dep_edges(edges: list[dict]) -> int:
    total = 0
    with sync_session() as session:
        for i in range(0, len(edges), _BATCH_SIZE_EDGES):
            batch = edges[i : i + _BATCH_SIZE_EDGES]
            result = session.run(_MERGE_PNPM_DEP, batch=batch)
            record = result.single()
            total += record["written"] if record else 0
    return total
