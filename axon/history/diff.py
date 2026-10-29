"""
Graph diff engine: computes the structural diff between two CommitSnapshots.

Approach:
  - For targets: compare the set of BazelTarget labels that existed at sha1
    vs sha2 by filtering on edge valid_from/valid_until windows
  - For symbols: compare PropertyVersion records between the two timestamps
  - For edges: query DEPENDS_ON edges with valid_from_commit between sha1
    and sha2, and edges with valid_until_commit between sha1 and sha2
  - For API endpoints: compare ApiEndpoint PropertyVersion records for
    schema_hash changes

The diff is expensive (multiple Cypher queries). Cache in Redis after
first computation keyed by (sha1, sha2).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from axon.db.connection import async_session
from axon.history.models import (
    AddedTarget, RemovedTarget, ChangedSymbol,
    AddedEdge, RemovedEdge, ApiSchemaChange, GraphDiffResult,
)
from axon.history.reader import get_commit_timestamp

logger = logging.getLogger(__name__)


_TARGETS_ADDED_BETWEEN = """
MATCH (t:BazelTarget)
WHERE t.id IS NOT NULL
OPTIONAL MATCH (cs_from:CommitSnapshot)<-[:INTRODUCED_BY]-(t)
WHERE cs_from.commit_timestamp > $ts1
  AND cs_from.commit_timestamp <= $ts2
RETURN t.label AS label, t.kind AS kind,
       t.team_owner AS team, t.has_swagger_src AS has_swagger,
       t.is_genrule AS is_genrule,
       cs_from.sha AS added_at_commit
LIMIT 200
"""

_TARGETS_REMOVED_BETWEEN = """
MATCH (pv:PropertyVersion)
WHERE pv.node_label = 'BazelTarget'
  AND pv.property_name = 'kind'
  AND pv.new_value = 'REMOVED'
MATCH (pv)-[:CHANGED_IN]->(cs:CommitSnapshot)
WHERE cs.commit_timestamp > $ts1
  AND cs.commit_timestamp <= $ts2
RETURN pv.node_id AS node_id, cs.sha AS removed_at_commit
LIMIT 200
"""

_SYMBOLS_CHANGED_BETWEEN = """
MATCH (pv:PropertyVersion)
WHERE pv.node_label = 'Symbol'
  AND pv.property_name IN ['signature', 'cyclomatic_complexity']
MATCH (pv)-[:CHANGED_IN]->(cs:CommitSnapshot)
WHERE cs.commit_timestamp > $ts1
  AND cs.commit_timestamp <= $ts2
MATCH (sym:Symbol {id: pv.node_id})
OPTIONAL MATCH (sf:SourceFile {path: sym.file_path})
OPTIONAL MATCH (sf)<-[:BUILDS|PRODUCES]-(t:BazelTarget)
RETURN
    pv.node_id          AS symbol_id,
    sym.name            AS name,
    sym.file_path       AS file_path,
    t.label             AS bazel_label,
    pv.property_name    AS property_changed,
    pv.old_value        AS old_value,
    pv.new_value        AS new_value,
    pv.is_breaking      AS is_breaking
ORDER BY cs.commit_timestamp ASC
LIMIT 500
"""

_EDGES_ADDED_BETWEEN = """
MATCH (from:BazelTarget)-[r:DEPENDS_ON]->(to:BazelTarget)
WHERE r.valid_from_commit IS NOT NULL
MATCH (cs:CommitSnapshot {sha: r.valid_from_commit})
WHERE cs.commit_timestamp > $ts1
  AND cs.commit_timestamp <= $ts2
RETURN
    from.label          AS from_label,
    to.label            AS to_label,
    type(r)             AS edge_type,
    r.valid_from_commit AS from_commit
LIMIT 500
"""

_EDGES_REMOVED_BETWEEN = """
MATCH (from:BazelTarget)-[r:DEPENDS_ON]->(to:BazelTarget)
WHERE r.valid_until_commit IS NOT NULL
MATCH (cs:CommitSnapshot {sha: r.valid_until_commit})
WHERE cs.commit_timestamp > $ts1
  AND cs.commit_timestamp <= $ts2
RETURN
    from.label           AS from_label,
    to.label             AS to_label,
    type(r)              AS edge_type,
    r.valid_until_commit AS until_commit
LIMIT 500
"""

_API_CHANGES_BETWEEN = """
MATCH (pv:PropertyVersion {property_name: 'schema_hash'})
WHERE pv.node_label = 'ApiEndpoint'
MATCH (pv)-[:CHANGED_IN]->(cs:CommitSnapshot)
WHERE cs.commit_timestamp > $ts1
  AND cs.commit_timestamp <= $ts2
MATCH (ep:ApiEndpoint {id: pv.node_id})
RETURN
    pv.node_id       AS endpoint_id,
    ep.http_method   AS http_method,
    ep.path          AS path,
    pv.is_breaking   AS is_breaking,
    'SCHEMA_CHANGED' AS change_type
LIMIT 200
"""


async def compute_diff(sha1: str, sha2: str) -> GraphDiffResult:
    """
    Compute the graph diff between two commit SHAs.
    sha1 is the older commit, sha2 the newer.
    """
    ts1 = await get_commit_timestamp(sha1)
    ts2 = await get_commit_timestamp(sha2)

    if not ts1 or not ts2:
        missing = sha1 if not ts1 else sha2
        raise ValueError(
            f"Commit {missing} not found in graph. "
            f"It may not have been indexed yet."
        )

    if ts1 >= ts2:
        raise ValueError(
            f"sha1 ({sha1[:8]}) must be older than sha2 ({sha2[:8]}). "
            f"Got ts1={ts1.isoformat()}, ts2={ts2.isoformat()}"
        )

    logger.info(
        "Computing graph diff: %s..%s (%s → %s)",
        sha1[:8], sha2[:8], ts1.date(), ts2.date()
    )

    async with async_session() as session:
        # ── Added targets ─────────────────────────────────────────────────────
        r = await session.run(_TARGETS_ADDED_BETWEEN, ts1=ts1, ts2=ts2)
        added_recs = await r.data()
        added_targets = [
            AddedTarget(
                label=rec["label"],
                kind=rec["kind"],
                team=rec["team"],
                has_swagger_src=rec.get("has_swagger", False),
                is_genrule=rec.get("is_genrule", False),
            )
            for rec in added_recs
            if rec.get("added_at_commit")  # only those introduced in window
        ]

        # ── Removed targets ───────────────────────────────────────────────────
        r = await session.run(_TARGETS_REMOVED_BETWEEN, ts1=ts1, ts2=ts2)
        removed_recs = await r.data()
        removed_targets = [
            RemovedTarget(
                label=rec["node_id"].replace("target::", ""),
                kind="UNKNOWN",
                team=None,
                last_seen_commit=rec["removed_at_commit"],
            )
            for rec in removed_recs
        ]

        # ── Changed symbols ───────────────────────────────────────────────────
        r = await session.run(_SYMBOLS_CHANGED_BETWEEN, ts1=ts1, ts2=ts2)
        changed_recs = await r.data()
        seen_syms: set[str] = set()
        changed_symbols = []
        for rec in changed_recs:
            key = f"{rec['symbol_id']}::{rec['property_changed']}"
            if key in seen_syms:
                continue
            seen_syms.add(key)
            changed_symbols.append(ChangedSymbol(
                symbol_id=rec["symbol_id"],
                name=rec["name"],
                file_path=rec["file_path"],
                bazel_label=rec.get("bazel_label"),
                property_changed=rec["property_changed"],
                old_value=rec["old_value"],
                new_value=rec["new_value"],
                is_breaking=rec.get("is_breaking", False),
            ))

        # ── Added edges ───────────────────────────────────────────────────────
        r = await session.run(_EDGES_ADDED_BETWEEN, ts1=ts1, ts2=ts2)
        added_edge_recs = await r.data()
        added_edges = [
            AddedEdge(
                from_label=rec["from_label"],
                to_label=rec["to_label"],
                edge_type=rec["edge_type"],
                from_commit=rec["from_commit"],
            )
            for rec in added_edge_recs
        ]

        # ── Removed edges ─────────────────────────────────────────────────────
        r = await session.run(_EDGES_REMOVED_BETWEEN, ts1=ts1, ts2=ts2)
        removed_edge_recs = await r.data()
        removed_edges = [
            RemovedEdge(
                from_label=rec["from_label"],
                to_label=rec["to_label"],
                edge_type=rec["edge_type"],
                until_commit=rec["until_commit"],
            )
            for rec in removed_edge_recs
        ]

        # ── API schema changes ────────────────────────────────────────────────
        r = await session.run(_API_CHANGES_BETWEEN, ts1=ts1, ts2=ts2)
        api_recs = await r.data()
        api_changes = [
            ApiSchemaChange(
                endpoint_id=rec["endpoint_id"],
                http_method=rec["http_method"],
                path=rec["path"],
                change_type=rec["change_type"],
                is_breaking=rec.get("is_breaking", False),
                details=None,
            )
            for rec in api_recs
        ]

    return GraphDiffResult(
        sha1=sha1,
        sha2=sha2,
        sha1_timestamp=ts1,
        sha2_timestamp=ts2,
        added_targets=added_targets,
        removed_targets=removed_targets,
        changed_symbols=changed_symbols,
        added_edges=added_edges,
        removed_edges=removed_edges,
        api_schema_changes=api_changes,
        summary={
            "added_targets": len(added_targets),
            "removed_targets": len(removed_targets),
            "changed_symbols": len(changed_symbols),
            "breaking_symbol_changes": sum(
                1 for s in changed_symbols if s.is_breaking
            ),
            "added_edges": len(added_edges),
            "removed_edges": len(removed_edges),
            "api_schema_changes": len(api_changes),
            "breaking_api_changes": sum(
                1 for a in api_changes if a.is_breaking
            ),
        },
    )