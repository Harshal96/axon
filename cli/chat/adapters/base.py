from __future__ import annotations

from typing import Protocol


class ChatAdapter(Protocol):
    async def query(self, text: str, limit: int = 10) -> dict:
        """Return normalized query results."""
