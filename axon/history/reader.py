"""
HistoryReader: reconstructs historical graph state from PropertyVersion nodes.

The key insight: we never store full graph snapshots. Instead, we reconstruct
the state at any commit by:
  1. Taking the current live graph as the base
  2. Walking the PropertyVersion chain backward to find the value each
     tracked property had at the requested commit
  3. Filtering edges by their valid_from_commit and valid_until_commit range

For blast radius time-travel: we don't re-run Cypher against a separate
historical graph. We run the same blast radius Cypher but with additional
WHERE clauses that filter by edge validity at the requested commit.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from axon.db.connection import async_session

logger = logging.getLogger(__name__)


_FETCH_COMMIT_TIMESTAMP = """
MATCH (cs:CommitSnapshot {sha: $sha})
RETURN cs.commit_timestamp AS ts, cs.index_success AS success
"""

_FETCH_PROPERTY_AT_COMMIT = """
MATCH (pv:PropertyVersion {node_id: $node_id, property_name: $prop})
MATCH (pv)-[:CHANGED_IN]->(cs:CommitSnapshot)
WHERE cs.commit_timestamp <= $at_timestamp
RETURN pv.new_value AS value, cs.commit_timestamp AS changed_at
ORDER BY cs.commit_timestamp DESC
LIMIT 1
"""

_BLAST_RADIUS_AT_COMMIT = """
MATCH (root:BazelTarget {label: $label})
CALL apoc.path.expandConfig(root, {
    relationshipFilter: '<DEPENDS_ON',
    minLevel: 1,
    maxLevel: $depth,
    uniqueness: 'NODE_GLOBAL'
}) YIELD path
WITH last(nodes(path)) AS affected, length(path) AS depth,
     relationships(path) AS rels
WHERE all(r IN rels WHERE
    r.valid_from_commit IS NOT NULL
    AND r.valid_until_commit IS NULL
    OR r.valid_until_commit = $at_commit
    OR (
        r.valid_from_commit <= $at_commit
        AND (r.valid_until_commit IS NULL OR r.valid_until_commit >= $at_commit)
    )
)
RETURN
    affected.label      AS label,
    affected.kind       AS kind,
    affected.team_owner AS team,
    affected.is_genrule AS is_genrule,
    depth,
    CASE WHEN depth = 1 THEN 'DIRECT' ELSE 'TRANSITIVE' END AS dep_type
ORDER BY depth ASC, affected.label ASC
LIMIT 500
"""

_SYMBOL_HISTORY = """
MATCH (pv:PropertyVersion {node_id: $node_id})
MATCH (pv)-[:CHANGED_IN]->(cs:CommitSnapshot)
RETURN
    pv.property_name    AS property_name,
    pv.old_value        AS old_value,
    pv.new_value        AS new_value,
    pv.is_breaking      AS is_breaking,
    pv.author           AS author,
    cs.sha              AS commit_sha,
    cs.commit_timestamp AS commit_timestamp
ORDER BY cs.commit_timestamp ASC
"""

_SYMBOL_CURRENT = """
MATCH (s:Symbol {id: $node_id})
MATCH (sf:SourceFile {path: s.file_path})
OPTIONAL MATCH (sf)<-[:BUILDS|PRODUCES]-(t:BazelTarget)
OPTIONAL MATCH (cs:CommitSnapshot)<-[:INTRODUCED_BY]-(s)
RETURN
    s.name              AS name,
    s.signature         AS signature,
    s.file_path         AS file_path,
    t.label             AS bazel_label,
    cs.sha              AS introduced_commit
"""

_API_ENDPOINT_HISTORY = """
MATCH (pv:PropertyVersion {node_id: $node_id, property_name: 'schema_hash'})
MATCH (pv)-[:CHANGED_IN]->(cs:CommitSnapshot)
RETURN
    pv.old_value        AS schema_hash_old,
    pv.new_value        AS schema_hash_new,
    pv.is_breaking      AS is_breaking,
    pv.author           AS author,
    cs.sha              AS commit_sha,
    cs.commit_timestamp AS commit_timestamp
ORDER BY cs.commit_timestamp ASC
"""

_API_ENDPOINT_CURRENT = """
MATCH (ep:ApiEndpoint {id: $node_id})
OPTIONAL MATCH (cs:CommitSnapshot)<-[:INTRODUCED_BY]-(ep)
RETURN
    ep.http_method      AS method,
    ep.path             AS path,
    ep.service_label    AS service_label,
    ep.schema_hash      AS current_hash,
    cs.sha              AS introduced_commit
"""

_COMMITS_IN_RANGE = """
MATCH (cs:CommitSnapshot)
WHERE cs.index_success = true
  AND ($branch IS NULL OR cs.branch = $branch)
RETURN cs.sha AS sha, cs.commit_timestamp AS ts,
       cs.author AS author, cs.branch AS branch,
       cs.changed_targets AS changed_targets,
       cs.breaking_api_changes AS breaking_api_changes
ORDER BY cs.commit_timestamp DESC
LIMIT $limit
"""


async def get_commit_timestamp(sha: str) -> Optional[datetime]:
    async with async_session() as session:
        result = await session.run(_FETCH_COMMIT_TIMESTAMP, sha=sha)
        record = await result.single()
        if not record or not record["success"]:
            return None
        return record["ts"]


async def get_property_at_commit(
    node_id: str,
    property_name: str,
    at_commit: str,
) -> Optional[str]:
    """
    Returns the value of a tracked property at a given commit SHA.
    Returns None if the property has never been versioned (use current value).
    """
    ts = await get_commit_timestamp(at_commit)
    if not ts:
        return None

    async with async_session() as session:
        result = await session.run(
            _FETCH_PROPERTY_AT_COMMIT,
            node_id=node_id,
            prop=property_name,
            at_timestamp=ts,
        )
        record = await result.single()
        return record["value"] if record else None


async def blast_radius_at_commit(
    label: str,
    at_commit: str,
    depth: int = 5,
) -> tuple[list[dict], int]:
    """
    Runs blast radius using only edges that were valid at `at_commit`.

    Returns (records, delta_nodes_consulted).
    """
    async with async_session() as session:
        result = await session.run(
            _BLAST_RADIUS_AT_COMMIT,
            label=label,
            depth=depth,
            at_commit=at_commit,
        )
        records = await result.data()
    return records, len(records)


async def get_symbol_history(node_id: str) -> dict:
    async with async_session() as session:
        curr_result = await session.run(_SYMBOL_CURRENT, node_id=node_id)
        curr = await curr_result.single()
        if not curr:
            return {}

        hist_result = await session.run(_SYMBOL_HISTORY, node_id=node_id)
        history = await hist_result.data()

    return {
        "name": curr["name"],
        "signature": curr["signature"],
        "file_path": curr["file_path"],
        "bazel_label": curr["bazel_label"],
        "introduced_commit": curr["introduced_commit"],
        "history": history,
    }


async def get_api_endpoint_history(node_id: str) -> dict:
    async with async_session() as session:
        curr_result = await session.run(_API_ENDPOINT_CURRENT, node_id=node_id)
        curr = await curr_result.single()
        if not curr:
            return {}

        hist_result = await session.run(_API_ENDPOINT_HISTORY, node_id=node_id)
        history = await hist_result.data()

    return {
        "method": curr["method"],
        "path": curr["path"],
        "service_label": curr["service_label"],
        "current_hash": curr["current_hash"],
        "introduced_commit": curr["introduced_commit"],
        "history": history,
    }


async def list_commits(
    limit: int = 50,
    branch: Optional[str] = None,
) -> list[dict]:
    async with async_session() as session:
        result = await session.run(
            _COMMITS_IN_RANGE, limit=limit, branch=branch
        )
        return await result.data()