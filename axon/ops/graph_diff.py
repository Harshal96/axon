from __future__ import annotations

import json
import time

from axon.history.diff import compute_diff
from axon.history.models import GraphDiffResult
from axon.ops.staleness import get_graph_meta


async def graph_diff(sha1: str, sha2: str) -> GraphDiffResult:
    """Op wrapper: compute diff and annotate with GraphMeta."""
    start = time.monotonic()
    result = await compute_diff(sha1, sha2)
    meta = await get_graph_meta()
    meta.query_duration_ms = int((time.monotonic() - start) * 1000)
    # GraphDiffResult doesn't carry GraphMeta directly — it has its own
    # sha1/sha2 timestamps. The HTTP layer wraps with meta separately.
    return result