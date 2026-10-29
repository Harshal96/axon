from __future__ import annotations

import time
from typing import Optional

from axon.history.models import SymbolHistoryEntry, SymbolHistoryResult
from axon.history.reader import get_symbol_history
from axon.ops.staleness import get_graph_meta


async def symbol_history(symbol_id: str) -> SymbolHistoryResult:
    start = time.monotonic()
    meta = await get_graph_meta()
    data = await get_symbol_history(symbol_id)

    if not data:
        raise ValueError(f"Symbol not found: {symbol_id}")

    entries = [
        SymbolHistoryEntry(
            commit_sha=h["commit_sha"],
            commit_timestamp=h.get("commit_timestamp"),
            author=h.get("author"),
            property_name=h["property_name"],
            old_value=h.get("old_value"),
            new_value=h["new_value"],
            is_breaking=h.get("is_breaking", False),
        )
        for h in data["history"]
        if h["property_name"] == "signature"   # primary: show signature history
    ]

    meta.query_duration_ms = int((time.monotonic() - start) * 1000)

    return SymbolHistoryResult(
        symbol_id=symbol_id,
        symbol_name=data.get("name", ""),
        current_signature=data.get("signature"),
        current_file=data.get("file_path", ""),
        introduced_at_commit=data.get("introduced_commit"),
        history=entries,
        total_changes=len(entries),
    )