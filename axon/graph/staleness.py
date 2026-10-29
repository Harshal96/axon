"""
Shared staleness utility. Every operation calls get_graph_meta()
to populate the GraphMeta envelope with graph_version, head_commit,
and staleness indicators.
"""

from __future__ import annotations

import logging
import subprocess
import time
from datetime import UTC, datetime

from axon.db.queries import get_latest_commit_snapshot
from axon.graph.models import GraphMeta

logger = logging.getLogger(__name__)

_HEAD_CACHE: tuple[str, float] | None = None


def _get_head_commit() -> str | None:
    """
    Returns HEAD SHA from git. Cached for 10 seconds to avoid
    spawning a subprocess on every single graph operation.
    """
    global _HEAD_CACHE
    now = time.monotonic()
    if _HEAD_CACHE and (now - _HEAD_CACHE[1]) < 10:
        return _HEAD_CACHE[0]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        sha = result.stdout.strip() if result.returncode == 0 else None
        if sha:
            _HEAD_CACHE = (sha, now)
        return sha
    except Exception:
        return None


async def get_graph_meta() -> GraphMeta:
    """
    Builds the GraphMeta for a response:
    - graph_version: SHA of the last successfully indexed commit
    - head_commit: current HEAD SHA from git (cached 10s)
    - is_stale: True if head_commit != graph_version or lag > threshold
    """
    latest = await get_latest_commit_snapshot()

    graph_version: str | None = None
    indexed_at: datetime | None = None

    if latest:
        graph_version = latest.get("sha")
        indexed_at = latest.get("index_completed_at")
        if indexed_at and indexed_at.tzinfo is None:
            indexed_at = indexed_at.replace(tzinfo=UTC)

    head_commit = _get_head_commit()

    stale_by_seconds: int | None = None
    is_stale = False

    if indexed_at:
        now = datetime.now(tz=UTC)
        stale_by_seconds = int((now - indexed_at).total_seconds())
        is_stale = stale_by_seconds > 300

    if head_commit and graph_version and head_commit != graph_version:
        is_stale = True

    return GraphMeta(
        graph_version=graph_version,
        graph_indexed_at=indexed_at,
        head_commit=head_commit,
        is_stale=is_stale,
        stale_by_seconds=stale_by_seconds,
    )
