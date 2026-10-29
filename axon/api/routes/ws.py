from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
_clients: set[WebSocket] = set()


@router.websocket("/ws")
async def graph_updates(ws: WebSocket) -> None:
    await ws.accept()
    _clients.add(ws)
    try:
        while True:
            message = await ws.receive_text()
            if message == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)


async def broadcast_graph_updated() -> None:
    if not _clients:
        return
    payload = json.dumps(
        {
            "type": "GRAPH_UPDATED",
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }
    )
    stale_clients: list[WebSocket] = []
    for client in list(_clients):
        try:
            await client.send_text(payload)
        except Exception:
            stale_clients.append(client)
    for client in stale_clients:
        _clients.discard(client)
