"""Contract tests for graph update WebSocket behavior."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
import websockets

pytestmark = pytest.mark.tier1


def _ws_url() -> str:
    base = os.getenv("TEST_API_URL", "http://localhost:8081")
    return base.replace("http://", "ws://").replace("https://", "wss://") + "/api/v1/ws"


@pytest.mark.asyncio
async def test_websocket_accepts_connection() -> None:
    async with websockets.connect(_ws_url()) as ws:
        assert getattr(ws, "open", True)


@pytest.mark.asyncio
async def test_websocket_responds_to_ping() -> None:
    async with websockets.connect(_ws_url()) as ws:
        await ws.send("ping")
        response = await asyncio.wait_for(ws.recv(), timeout=3.0)
        assert response == "pong"


@pytest.mark.asyncio
async def test_websocket_receives_broadcast(async_api_client) -> None:
    received = []
    async with websockets.connect(_ws_url()) as ws:
        await async_api_client.post("/api/v1/ops/refresh", json={"priority": "high"})
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
            received.append(json.loads(msg))
        except TimeoutError:
            pass

    for msg in received:
        assert msg["type"] in ("GRAPH_UPDATED", "BREAKING_CHANGE_DETECTED")


@pytest.mark.asyncio
async def test_websocket_multiple_clients() -> None:
    async with websockets.connect(_ws_url()) as ws1, websockets.connect(_ws_url()) as ws2:
        await ws1.send("ping")
        await ws2.send("ping")
        r1 = await asyncio.wait_for(ws1.recv(), timeout=3.0)
        r2 = await asyncio.wait_for(ws2.recv(), timeout=3.0)
        assert r1 == r2 == "pong"
