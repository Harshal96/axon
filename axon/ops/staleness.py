from typing import Optional
from pydantic import BaseModel

class GraphMeta(BaseModel):
    graph_version: str
    is_stale: bool
    query_duration_ms: int = 0
    nodes_traversed: int = 0

def _get_head_commit() -> Optional[str]:
    return "0000000000000000000000000000000000000000"

async def get_graph_meta() -> GraphMeta:
    return GraphMeta(graph_version=_get_head_commit() or "unknown", is_stale=False)
