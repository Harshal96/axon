"""
Standard response envelope helpers for graph operations.
Operations now return typed Pydantic models from models.py.
This module provides the timed_operation helper for populating GraphMeta.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from axon.graph.models import GraphMeta
from axon.graph.staleness import get_graph_meta

T = TypeVar("T")


async def timed_operation(
    depth: int,
    fn: Callable[[], Awaitable[T]],
) -> T:
    """
    Executes an async graph operation, timing it and populating meta.
    fn must return a Pydantic model with a `meta: GraphMeta` field.
    The returned model's meta will be updated with query_duration_ms and depth_used.
    """
    try:
        meta = await get_graph_meta()
    except Exception:
        meta = GraphMeta()

    start = time.monotonic()
    result = await fn(meta)
    elapsed_ms = int((time.monotonic() - start) * 1000)

    result.meta.query_duration_ms = elapsed_ms
    result.meta.depth_used = depth
    return result
