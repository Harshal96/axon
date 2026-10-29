"""
Schema initialization for axon's Neo4j graph.

Creates all uniqueness constraints and indexes.
Safe to run multiple times (uses IF NOT EXISTS).

Usage:
    uv run axon-init
    uv run python -m axon.db.init
"""

from __future__ import annotations

import logging
import sys
from typing import NamedTuple

from axon.db.connection import sync_session

logger = logging.getLogger(__name__)


class ConstraintDef(NamedTuple):
    name: str
    label: str
    property: str


class IndexDef(NamedTuple):
    name: str
    label: str
    properties: list[str]
    index_type: str = "BTREE"  # BTREE | FULLTEXT


# ── Uniqueness constraints (also create implicit indexes) ──────────────────────
#
# We use the node_id computed property as the unique key for all nodes.
# This is the value written to a `id` property in Neo4j.

CONSTRAINTS: list[ConstraintDef] = [
    ConstraintDef("constraint_source_file_id", "SourceFile", "id"),
    ConstraintDef("constraint_bazel_target_id", "BazelTarget", "id"),
    ConstraintDef("constraint_symbol_id", "Symbol", "id"),
    ConstraintDef("constraint_api_endpoint_id", "ApiEndpoint", "id"),
    ConstraintDef("constraint_swagger_id", "SwaggerSchema", "id"),
    ConstraintDef("constraint_gen_client_id", "GeneratedClient", "id"),
    ConstraintDef("constraint_maven_id", "MavenArtifact", "id"),
    ConstraintDef("constraint_pnpm_id", "PnpmPackage", "id"),
    ConstraintDef("constraint_team_id", "Team", "id"),
    ConstraintDef("constraint_commit_id", "CommitSnapshot", "id"),
    ConstraintDef("constraint_property_version_id", "PropertyVersion", "id"),
    ConstraintDef("constraint_breaking_alert_id", "BreakingChangeAlert", "id"),
]


# ── Additional lookup indexes ──────────────────────────────────────────────────

BTREE_INDEXES: list[IndexDef] = [
    # SourceFile — frequently queried by path and by is_generated flag
    IndexDef("idx_source_file_path", "SourceFile", ["path"]),
    IndexDef("idx_source_file_generated", "SourceFile", ["is_generated"]),
    IndexDef("idx_source_file_language", "SourceFile", ["language"]),
    IndexDef("idx_source_file_sha256", "SourceFile", ["sha256"]),
    # BazelTarget — heavily queried by label, kind, package, team
    IndexDef("idx_bazel_label", "BazelTarget", ["label"]),
    IndexDef("idx_bazel_package", "BazelTarget", ["package"]),
    IndexDef("idx_bazel_kind", "BazelTarget", ["kind"]),
    IndexDef("idx_bazel_team", "BazelTarget", ["team_owner"]),
    IndexDef("idx_bazel_testonly", "BazelTarget", ["testonly"]),
    IndexDef("idx_bazel_is_genrule", "BazelTarget", ["is_genrule"]),
    # Symbol — queried by qualified name, language, kind, file
    IndexDef("idx_symbol_qualified_name", "Symbol", ["qualified_name"]),
    IndexDef("idx_symbol_name", "Symbol", ["name"]),
    IndexDef("idx_symbol_file_path", "Symbol", ["file_path"]),
    IndexDef("idx_symbol_kind", "Symbol", ["kind"]),
    IndexDef("idx_symbol_language", "Symbol", ["language"]),
    IndexDef("idx_symbol_is_entry_point", "Symbol", ["is_entry_point"]),
    IndexDef("idx_symbol_is_test", "Symbol", ["is_test"]),
    # ApiEndpoint — queried by path, service, method
    IndexDef("idx_endpoint_path", "ApiEndpoint", ["path"]),
    IndexDef("idx_endpoint_service", "ApiEndpoint", ["service_label"]),
    IndexDef("idx_endpoint_method", "ApiEndpoint", ["http_method"]),
    IndexDef("idx_endpoint_swagger", "ApiEndpoint", ["swagger_source"]),
    IndexDef("idx_endpoint_deprecated", "ApiEndpoint", ["is_deprecated"]),
    # SwaggerSchema — queried by path and content_hash (for change detection)
    IndexDef("idx_swagger_path", "SwaggerSchema", ["path"]),
    IndexDef("idx_swagger_service", "SwaggerSchema", ["service_label"]),
    IndexDef("idx_swagger_hash", "SwaggerSchema", ["content_hash"]),
    # GeneratedClient — queried by source swagger and needs_regeneration
    IndexDef("idx_genclient_swagger", "GeneratedClient", ["source_swagger"]),
    IndexDef("idx_genclient_regen", "GeneratedClient", ["needs_regeneration"]),
    IndexDef("idx_genclient_language", "GeneratedClient", ["target_language"]),
    # CommitSnapshot — queried by sha and index_success
    IndexDef("idx_commit_sha", "CommitSnapshot", ["sha"]),
    IndexDef("idx_commit_success", "CommitSnapshot", ["index_success"]),
    IndexDef("idx_commit_branch", "CommitSnapshot", ["branch"]),
    IndexDef("idx_propver_node_id",   "PropertyVersion", ["node_id"]),
    IndexDef("idx_propver_prop_name", "PropertyVersion", ["property_name"]),
    IndexDef("idx_propver_node_prop", "PropertyVersion", ["node_id", "property_name"]),
    IndexDef("idx_propver_commit",    "PropertyVersion", ["changed_at_commit"]),
    IndexDef("idx_commit_day_summary", "CommitDaySummary", ["day"]),
    IndexDef("idx_alert_commit",        "BreakingChangeAlert", ["commit_sha"]),
    IndexDef("idx_alert_severity",      "BreakingChangeAlert", ["severity"]),
    IndexDef("idx_alert_acknowledged",  "BreakingChangeAlert", ["is_acknowledged"]),
    IndexDef("idx_alert_type",          "BreakingChangeAlert", ["alert_type"]),
]


FULLTEXT_INDEXES: list[IndexDef] = [
    # Full-text search over symbols — used by semantic_search operation
    IndexDef(
        "idx_symbol_fulltext",
        "Symbol",
        ["name", "qualified_name", "docstring", "signature"],
        index_type="FULLTEXT",
    ),
    # Full-text search over Bazel targets — used for target discovery
    IndexDef(
        "idx_bazel_fulltext",
        "BazelTarget",
        ["label", "name", "package"],
        index_type="FULLTEXT",
    ),
    # Full-text search over API endpoints
    IndexDef(
        "idx_endpoint_fulltext",
        "ApiEndpoint",
        ["path", "operation_id", "summary"],
        index_type="FULLTEXT",
    ),
]


def create_constraints(session: object) -> None:
    for c in CONSTRAINTS:
        cypher = (
            f"CREATE CONSTRAINT {c.name} IF NOT EXISTS "
            f"FOR (n:{c.label}) REQUIRE n.{c.property} IS UNIQUE"
        )
        session.run(cypher)
        logger.debug("Constraint ensured: %s", c.name)


def create_btree_indexes(session: object) -> None:
    for idx in BTREE_INDEXES:
        props = ", ".join(f"n.{p}" for p in idx.properties)
        cypher = f"CREATE INDEX {idx.name} IF NOT EXISTS FOR (n:{idx.label}) ON ({props})"
        session.run(cypher)
        logger.debug("Index ensured: %s", idx.name)


def create_fulltext_indexes(session: object) -> None:
    for idx in FULLTEXT_INDEXES:
        props = ", ".join(f"n.{p}" for p in idx.properties)
        cypher = (
            f"CREATE FULLTEXT INDEX {idx.name} IF NOT EXISTS FOR (n:{idx.label}) ON EACH [{props}]"
        )
        session.run(cypher)
        logger.debug("Fulltext index ensured: %s", idx.name)


def initialize_schema() -> None:
    """
    Idempotent schema initialization. Safe to call multiple times.
    Runs in a single session with explicit transaction control.
    """
    logger.info("Initializing axon graph schema...")

    with sync_session() as session:
        logger.info("Creating uniqueness constraints (%d)...", len(CONSTRAINTS))
        create_constraints(session)

        logger.info("Creating btree indexes (%d)...", len(BTREE_INDEXES))
        create_btree_indexes(session)

        logger.info("Creating fulltext indexes (%d)...", len(FULLTEXT_INDEXES))
        create_fulltext_indexes(session)

    logger.info(
        "Schema initialized: %d constraints, %d btree indexes, %d fulltext indexes",
        len(CONSTRAINTS),
        len(BTREE_INDEXES),
        len(FULLTEXT_INDEXES),
    )


def verify_schema() -> dict:
    """
    Returns a summary of existing constraints and indexes.
    Used by /health endpoint and the test suite.
    """
    with sync_session() as session:
        constraints = session.run("SHOW CONSTRAINTS").data()
        indexes = session.run("SHOW INDEXES").data()
    return {
        "constraint_count": len(constraints),
        "index_count": len(indexes),
        "constraints": [c.get("name") for c in constraints],
        "indexes": [i.get("name") for i in indexes],
    }


def main() -> None:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    try:
        initialize_schema()
        summary = verify_schema()
        print(
            f"\nSchema initialized successfully.\n"
            f"  Constraints: {summary['constraint_count']}\n"
            f"  Indexes:     {summary['index_count']}\n"
        )
    except Exception as e:
        logger.error("Schema initialization failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
