from __future__ import annotations

import httpx


class RemoteChatAdapter:
    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    async def query(self, text: str, limit: int = 10) -> dict:
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{self.base_url}/api/v1/ops/search",
                params={"q": text, "limit": limit},
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()
