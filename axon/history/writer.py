"""
HistoryWriter: called by the ingestion pipeline coordinator after each
successful index cycle.

Responsibilities:
  1. Compare the newly indexed state of changed nodes against the previous
     state (stored as PropertyVersion nodes or read from the previous
     CommitSnapshot)
  2. For each property that changed: write a new PropertyVersion node and
     link it to the CommitSnapshot
  3. For each edge that was removed: set valid_until_commit on the edge
  4. For each edge that was added: confirm valid_from_commit is set
     (already done by the workers via the $valid_from_commit parameter)

This writer is designed to be fast: it only processes nodes listed in
the ingestion event's changed_targets list. It never re-processes the
entire graph.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from axon.db.connection import async_session, sync_session

logger = logging.getLogger(__name__)

# Properties we track historically for each node label
TRACKED_PROPERTIES: dict[str, list[str]] = {
    "Symbol": ["signature", "docstring", "cyclomatic_complexity", "file_path"],
    "BazelTarget": ["visibility", "kind", "team_owner"],
    "ApiEndpoint": ["schema_hash", "is_deprecated", "path"],
    "SwaggerSchema": ["content_hash", "endpoint_count"],
    "GeneratedClient": ["needs_regeneration", "generated_at_commit"],
}

# Properties whose change is always a BREAKING change if the node is public
BREAKING_PROPERTIES: dict[str, set[str]] = {
    "Symbol": {"signature"},
    "ApiEndpoint": {"schema_hash", "path"},
    "SwaggerSchema": {"content_hash"},
}


def _version_id(node_id: str, prop: str, sha: str) -> str:
    raw = f"{node_id}::{prop}::{sha}"
    digest = hashlib.sha1(raw.encode()).hexdigest()[:12]
    return f"propver::{digest}"


def _serialize_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, sort_keys=True)
    return str(v)


# ── Cypher templates ───────────────────────────────────────────────────────────

_FETCH_CURRENT_PROPS = """
MATCH (n {id: $node_id})
RETURN properties(n) AS props, labels(n) AS labels
"""

_FETCH_LATEST_VERSION = """
MATCH (pv:PropertyVersion {node_id: $node_id, property_name: $prop})
RETURN pv.new_value AS value, pv.changed_at_commit AS commit
ORDER BY pv.changed_at DESC
LIMIT 1
"""

_MERGE_PROPERTY_VERSION = """
UNWIND $batch AS props
MERGE (pv:PropertyVersion {id: props.version_id})
ON CREATE SET
    pv.node_id          = props.node_id,
    pv.node_label       = props.node_label,
    pv.property_name    = props.property_name,
    pv.old_value        = props.old_value,
    pv.new_value        = props.new_value,
    pv.changed_at_commit = props.changed_at_commit,
    pv.changed_at       = datetime(props.changed_at),
    pv.author           = props.author,
    pv.is_breaking      = props.is_breaking,
    pv.created_at       = datetime()
RETURN count(pv) AS written
"""

_LINK_VERSION_TO_COMMIT = """
UNWIND $batch AS props
MATCH (pv:PropertyVersion {id: props.version_id})
MATCH (cs:CommitSnapshot  {sha: props.commit_sha})
MERGE (pv)-[:CHANGED_IN]->(cs)
RETURN count(*) AS linked
"""

_MARK_EDGES_REMOVED = """
UNWIND $batch AS props
MATCH (from {id: props.from_id})-[r]->(to {id: props.to_id})
WHERE type(r) = props.edge_type
  AND r.valid_until_commit IS NULL
SET r.valid_until_commit = props.commit_sha,
    r.valid_until_at     = datetime(props.valid_until_at)
RETURN count(r) AS marked
"""

_FETCH_ACTIVE_EDGES_FOR_TARGET = """
MATCH (t:BazelTarget {label: $label})-[r:DEPENDS_ON]->(dep:BazelTarget)
WHERE r.valid_until_commit IS NULL
RETURN dep.label AS dep_label, r.valid_from_commit AS from_commit
"""


async def record_changes(
    changed_target_labels: list[str],
    commit_sha: str,
    commit_timestamp: datetime,
    author: Optional[str],
) -> dict[str, int]:
    """
    Main entry point called by the pipeline coordinator after each index cycle.

    For each changed target:
      1. Fetch current properties from the live graph
      2. Compare against latest PropertyVersion for each tracked property
      3. Write new PropertyVersion nodes where values changed

    Returns counts: {property_versions_written, edges_marked_removed}
    """
    if not changed_target_labels:
        return {"property_versions_written": 0, "edges_marked_removed": 0}

    version_batch: list[dict] = []
    link_batch: list[dict] = []
    ts_str = commit_timestamp.isoformat()

    async with async_session() as session:
        for label in changed_target_labels:
            node_id = f"target::{label}"

            # ── Fetch current node properties ────────────────────────────────
            curr_result = await session.run(_FETCH_CURRENT_PROPS, node_id=node_id)
            curr_record = await curr_result.single()
            if not curr_record:
                continue

            props = curr_record["props"]
            labels = curr_record["labels"]
            node_label = next(
                (l for l in labels if l in TRACKED_PROPERTIES), None
            )
            if not node_label:
                continue

            tracked = TRACKED_PROPERTIES[node_label]
            breaking_props = BREAKING_PROPERTIES.get(node_label, set())

            for prop_name in tracked:
                current_val = _serialize_value(props.get(prop_name))

                # Fetch previous value from PropertyVersion chain
                prev_result = await session.run(
                    _FETCH_LATEST_VERSION, node_id=node_id, prop=prop_name
                )
                prev_record = await prev_result.single()
                previous_val = prev_record["value"] if prev_record else None

                if current_val == previous_val:
                    continue   # no change, skip

                is_breaking = (
                    prop_name in breaking_props
                    and previous_val is not None   # not first write
                    and props.get("is_public", True)
                )

                vid = _version_id(node_id, prop_name, commit_sha)
                version_batch.append({
                    "version_id": vid,
                    "node_id": node_id,
                    "node_label": node_label,
                    "property_name": prop_name,
                    "old_value": previous_val,
                    "new_value": current_val,
                    "changed_at_commit": commit_sha,
                    "changed_at": ts_str,
                    "author": author,
                    "is_breaking": is_breaking,
                })
                link_batch.append({
                    "version_id": vid,
                    "commit_sha": commit_sha,
                })

        # ── Also track Symbol property changes ────────────────────────────────
        # Symbols don't have a direct BazelTarget label, so we query by
        # changed source files within changed targets
        for label in changed_target_labels:
            sym_result = await session.run("""
                MATCH (t:BazelTarget {label: $label})
                MATCH (t)-[:BUILDS|PRODUCES]->(sf:SourceFile)
                MATCH (sf)-[:DEFINES]->(sym:Symbol)
                RETURN sym.id AS sym_id, sym.signature AS sig,
                       sym.cyclomatic_complexity AS cc,
                       sym.is_public AS is_public
            """, label=label)
            sym_records = await sym_result.data()

            for sr in sym_records:
                node_id = sr["sym_id"]
                for prop_name, current_raw in [
                    ("signature", sr.get("sig")),
                    ("cyclomatic_complexity", sr.get("cc")),
                ]:
                    current_val = _serialize_value(current_raw)
                    prev_result = await session.run(
                        _FETCH_LATEST_VERSION, node_id=node_id, prop=prop_name
                    )
                    prev_record = await prev_result.single()
                    previous_val = prev_record["value"] if prev_record else None

                    if current_val == previous_val:
                        continue

                    is_breaking = (
                        prop_name == "signature"
                        and previous_val is not None
                        and sr.get("is_public", True)
                    )
                    vid = _version_id(node_id, prop_name, commit_sha)
                    version_batch.append({
                        "version_id": vid,
                        "node_id": node_id,
                        "node_label": "Symbol",
                        "property_name": prop_name,
                        "old_value": previous_val,
                        "new_value": current_val,
                        "changed_at_commit": commit_sha,
                        "changed_at": ts_str,
                        "author": author,
                        "is_breaking": is_breaking,
                    })
                    link_batch.append({"version_id": vid, "commit_sha": commit_sha})

        # ── Write PropertyVersion nodes ───────────────────────────────────────
        versions_written = 0
        if version_batch:
            result = await session.run(_MERGE_PROPERTY_VERSION, batch=version_batch)
            record = await result.single()
            versions_written = record["written"] if record else 0

        # ── Link versions to CommitSnapshot ───────────────────────────────────
        if link_batch:
            await session.run(_LINK_VERSION_TO_COMMIT, batch=link_batch)

    # ── Phase 12: Detect and notify breaking changes ──────────────────────────
    from axon.breaking.detector import detect_and_write
    from axon.breaking.notifier import notify_breaking_changes
    from axon.api.ws import manager as ws_manager

    alerts = await detect_and_write(commit_sha, author)
    if alerts:
        await notify_breaking_changes(alerts, commit_sha, author)

        critical_count = sum(1 for a in alerts if a["severity"] == "CRITICAL")
        affected_teams = set()
        for a in alerts:
            affected_teams.update(a.get("affected_teams", []))

        await ws_manager.broadcast({
            "type": "BREAKING_CHANGE_DETECTED",
            "commit_sha": commit_sha,
            "alert_count": len(alerts),
            "critical_count": critical_count,
            "affected_teams": list(affected_teams),
            "alerts": [{"id": a["id"], "severity": a["severity"], "node_id": a["node_id"]} for a in alerts]
        })

    logger.info(
        "HistoryWriter: %d property versions written for commit %s",
        versions_written, commit_sha[:8],
    )
    return {
        "property_versions_written": versions_written,
        "edges_marked_removed": 0,   # Edge removal detection in detect_removed_edges()
    }


async def detect_removed_edges(
    target_label: str,
    current_dep_labels: list[str],
    commit_sha: str,
    commit_timestamp: datetime,
) -> int:
    """
    Compare the current declared deps of `target_label` against what was
    previously recorded as active DEPENDS_ON edges. For any edge that is
    no longer present, set valid_until_commit.

    Called by BazelWorker after writing new deps for a target.
    `current_dep_labels`: the labels of targets still declared in deps.
    """
    ts_str = commit_timestamp.isoformat()
    mark_batch: list[dict] = []

    async with async_session() as session:
        result = await session.run(
            _FETCH_ACTIVE_EDGES_FOR_TARGET, label=target_label
        )
        records = await result.data()

    previously_active = {r["dep_label"] for r in records}
    current_set = set(current_dep_labels)
    removed = previously_active - current_set

    for dep_label in removed:
        mark_batch.append({
            "from_id": f"target::{target_label}",
            "to_id": f"target::{dep_label}",
            "edge_type": "DEPENDS_ON",
            "commit_sha": commit_sha,
            "valid_until_at": ts_str,
        })

    if not mark_batch:
        return 0

    async with async_session() as session:
        result = await session.run(_MARK_EDGES_REMOVED, batch=mark_batch)
        record = await result.single()
        return record["marked"] if record else 0