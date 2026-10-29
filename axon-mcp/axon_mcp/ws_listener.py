import asyncio
import logging
import websockets
import json
from axon_mcp.cache import invalidate_all
from axon_mcp.config import load_config

logger = logging.getLogger(__name__)

async def listen():
    config = load_config()
    ws_url = config.api_url.replace("http", "ws") + "/ws/events"
    
    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                logger.info("Connected to WebSocket")
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if data.get("type") == "GRAPH_UPDATED":
                        logger.info("Graph updated, invalidating cache")
                        invalidate_all()
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            await asyncio.sleep(5)

def run():
    asyncio.run(listen())
