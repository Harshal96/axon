"""
HistoryCompactionJob: nightly maintenance job.

Prevents the PropertyVersion chain from growing unboundedly by:
  1. Deleting PropertyVersion nodes older than retention_days
  2. Aggregating daily commit counts into summary nodes
     (CommitSummary: one per calendar day, replaces individual CommitSnapshot
     metadata for days older than retention_days)
  3. Removing valid_until_commit edges older than retention_days
     (once an edge is long-removed, we no longer need it for time-travel)

This job runs as a scheduled Kubernetes CronJob (or APScheduler task) and
is separate from the ingestion pipeline — it must never interfere with
live indexing.

Default retention: 365 days for PropertyVersion nodes,
                   730 days for CommitSnapshot nodes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from axon.config import get_settings
from axon.db.connection import sync_session

logger = logging.getLogger(__name__)

_DELETE_OLD_PROPERTY_VERSIONS = """
MATCH (pv:PropertyVersion)-[:CHANGED_IN]->(cs:CommitSnapshot)
WHERE cs.commit_timestamp < $cutoff
WITH pv LIMIT $batch_size
DETACH DELETE pv
RETURN count(*) AS deleted
"""

_DELETE_OLD_REMOVED_EDGES = """
MATCH ()-[r:DEPENDS_ON]->()
WHERE r.valid_until_commit IS NOT NULL
  AND r.valid_until_at < $cutoff
WITH r LIMIT $batch_size
DELETE r
RETURN count(*) AS deleted
"""

_AGGREGATE_DAILY_COMMITS = """
MATCH (cs:CommitSnapshot)
WHERE cs.commit_timestamp < $cutoff
  AND cs.index_success = true
WITH date(cs.commit_timestamp) AS day,
     count(cs) AS commit_count,
     sum(size(cs.changed_targets)) AS total_changed_targets
MERGE (summary:CommitDaySummary {day: toString(day)})
ON CREATE SET
    summary.commit_count           = commit_count,
    summary.total_changed_targets  = total_changed_targets,
    summary.created_at             = datetime()
ON MATCH SET
    summary.commit_count           = commit_count,
    summary.total_changed_targets  = total_changed_targets
RETURN count(summary) AS summaries_written
"""

_DELETE_OLD_COMMIT_SNAPSHOTS = """
MATCH (cs:CommitSnapshot)
WHERE cs.commit_timestamp < $cutoff
WITH cs LIMIT $batch_size
DETACH DELETE cs
RETURN count(*) AS deleted
"""


def run_compaction(
    property_version_retention_days: int = 365,
    commit_snapshot_retention_days: int = 730,
    batch_size: int = 1000,
) -> dict[str, int]:
    """
    Run the full compaction cycle. Returns counts of deleted/aggregated records.
    Designed to be called from a CronJob or management CLI command.
    """
    now = datetime.now(tz=timezone.utc)
    pv_cutoff = now - timedelta(days=property_version_retention_days)
    cs_cutoff = now - timedelta(days=commit_snapshot_retention_days)

    logger.info(
        "Starting compaction: pv_cutoff=%s cs_cutoff=%s",
        pv_cutoff.date(), cs_cutoff.date()
    )

    total_pv_deleted = 0
    total_edge_deleted = 0
    total_cs_deleted = 0
    summaries_written = 0

    with sync_session() as session:
        # ── 1. Delete old PropertyVersion nodes in batches ────────────────────
        while True:
            result = session.run(
                _DELETE_OLD_PROPERTY_VERSIONS,
                cutoff=pv_cutoff,
                batch_size=batch_size,
            )
            record = result.single()
            deleted = record["deleted"] if record else 0
            total_pv_deleted += deleted
            if deleted < batch_size:
                break
        logger.info("Compaction: deleted %d old PropertyVersion nodes", total_pv_deleted)

        # ── 2. Delete old removed DEPENDS_ON edges ────────────────────────────
        while True:
            result = session.run(
                _DELETE_OLD_REMOVED_EDGES,
                cutoff=pv_cutoff,
                batch_size=batch_size,
            )
            record = result.single()
            deleted = record["deleted"] if record else 0
            total_edge_deleted += deleted
            if deleted < batch_size:
                break
        logger.info("Compaction: deleted %d old removed edges", total_edge_deleted)

        # ── 3. Aggregate daily commit summaries before deleting snapshots ─────
        result = session.run(_AGGREGATE_DAILY_COMMITS, cutoff=cs_cutoff)
        record = result.single()
        summaries_written = record["summaries_written"] if record else 0
        logger.info("Compaction: wrote %d CommitDaySummary nodes", summaries_written)

        # ── 4. Delete old CommitSnapshot nodes ────────────────────────────────
        while True:
            result = session.run(
                _DELETE_OLD_COMMIT_SNAPSHOTS,
                cutoff=cs_cutoff,
                batch_size=batch_size,
            )
            record = result.single()
            deleted = record["deleted"] if record else 0
            total_cs_deleted += deleted
            if deleted < batch_size:
                break
        logger.info("Compaction: deleted %d old CommitSnapshot nodes", total_cs_deleted)

    return {
        "property_versions_deleted": total_pv_deleted,
        "removed_edges_deleted": total_edge_deleted,
        "commit_snapshots_deleted": total_cs_deleted,
        "daily_summaries_written": summaries_written,
    }


def cli_main() -> None:
    import argparse
    import sys

    logging.basicConfig(level="INFO",
                        format="%(asctime)s %(levelname)s — %(message)s")
    ap = argparse.ArgumentParser(description="Axon history compaction")
    ap.add_argument("--pv-retention-days",  type=int, default=365)
    ap.add_argument("--cs-retention-days",  type=int, default=730)
    ap.add_argument("--batch-size",         type=int, default=1000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.dry_run:
        print("Dry run — no changes will be made")
        sys.exit(0)

    result = run_compaction(
        property_version_retention_days=args.pv_retention_days,
        commit_snapshot_retention_days=args.cs_retention_days,
        batch_size=args.batch_size,
    )
    print("\nCompaction complete:")
    for k, v in result.items():
        print(f"  {k}: {v}")