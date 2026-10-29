from __future__ import annotations

import time
from typing import Optional

from axon.history.models import ApiHistoryEntry, ApiEndpointHistoryResult
from axon.history.reader import get_api_endpoint_history
from axon.ops.staleness import get_graph_meta


async def api_endpoint_history(endpoint_id: str) -> ApiEndpointHistoryResult:
    start = time.monotonic()
    meta = await get_graph_meta()
    data = await get_api_endpoint_history(endpoint_id)

    if not data:
        raise ValueError(f"API endpoint not found: {endpoint_id}")

    entries = [
        ApiHistoryEntry(
            commit_sha=h["commit_sha"],
            commit_timestamp=h.get("commit_timestamp"),
            author=h.get("author"),
            schema_hash_old=h.get("schema_hash_old"),
            schema_hash_new=h["schema_hash_new"],
            is_breaking=h.get("is_breaking", False),
            breaking_changes=[],
        )
        for h in data["history"]
    ]

    meta.query_duration_ms = int((time.monotonic() - start) * 1000)

    return ApiEndpointHistoryResult(
        endpoint_id=endpoint_id,
        http_method=data.get("method", ""),
        path=data.get("path", ""),
        service_label=data.get("service_label", ""),
        introduced_at_commit=data.get("introduced_commit"),
        history=entries,
        total_breaking_changes=sum(1 for e in entries if e.is_breaking),
    )
