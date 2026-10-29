from __future__ import annotations

from axon.graph import search as graph_search


class LocalChatAdapter:
    async def query(self, text: str, limit: int = 10) -> dict:
        result = await graph_search.semantic_search(text, scope=None, limit=limit)
        return result.model_dump(mode="json")
