from __future__ import annotations

import time
from typing import Optional

from axon.history.models import TimeTravelBlastRadiusResult, TimeTravelMeta
from axon.history.reader import blast_radius_at_commit, get_commit_timestamp
from axon.ops.staleness import get_graph_meta, _get_head_commit


async def time_travel_blast_radius(
    label: str,
    at_commit: str,
    depth: int = 5,
) -> TimeTravelBlastRadiusResult:
    start = time.monotonic()
    meta = await get_graph_meta()

    at_ts = await get_commit_timestamp(at_commit)
    if not at_ts:
        raise ValueError(
            f"Commit {at_commit} not found in graph. "
            f"Only indexed commits can be used for time-travel queries."
        )

    records, nodes_consulted = await blast_radius_at_commit(label, at_commit, depth)

    # Count how many commits behind HEAD this is
    head = _get_head_commit()
    # Approximate: count CommitSnapshots between at_commit and HEAD
    from axon.db.connection import async_session
    async with async_session() as session:
        result = await session.run("""
            MATCH (cs:CommitSnapshot)
            WHERE cs.commit_timestamp > $at_ts
              AND cs.index_success = true
            RETURN count(cs) AS behind_count
        """, at_ts=at_ts)
        record = await result.single()
        commits_behind = record["behind_count"] if record else 0

    affected_targets = [
        {
            "label": rec["label"],
            "kind": rec["kind"],
            "team": rec["team"],
            "depth": rec["depth"],
            "dependency_type": rec["dep_type"],
            "impact_confidence": "EXTRACTED",
            "impact_score": 0.9 if rec["dep_type"] == "DIRECT" else 0.7,
            "is_generated": rec.get("is_genrule", False),
        }
        for rec in records
    ]

    team_counts: dict[str, int] = {}
    for t in affected_targets:
        team = t.get("team")
        if team:
            team_counts[team] = team_counts.get(team, 0) + 1

    meta.query_duration_ms = int((time.monotonic() - start) * 1000)
    meta.nodes_traversed = nodes_consulted
    meta.graph_version = at_commit
    meta.is_stale = False     # historical queries are always "fresh" for their commit

    tt_meta = TimeTravelMeta(
        at_commit=at_commit,
        at_commit_timestamp=at_ts,
        reconstructed_from_deltas=nodes_consulted,
        current_head=head or "",
        commits_behind_head=commits_behind,
    )

    return TimeTravelBlastRadiusResult(
        meta=meta.model_dump(),
        root_label=label,
        summary={
            "total_affected_targets": len(affected_targets),
            "total_affected_teams": len(team_counts),
            "at_commit": at_commit,
            "commits_behind_head": commits_behind,
        },
        affected_targets=affected_targets,
        affected_by_team=team_counts,
        cross_service_api_risks=[],
        warnings=[{
            "code": "TIME_TRAVEL_QUERY",
            "message": (
                f"This result reflects the graph state at commit {at_commit[:8]}, "
                f"which is {commits_behind} commit(s) behind HEAD. "
                f"Use GET /ops/blast-radius for the current state."
            ),
            "affected_nodes": [],
        }],
        time_travel=tt_meta,
    )