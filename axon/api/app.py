from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from axon.api.routes import health
from axon.config import get_settings
from axon.db.connection import close_drivers, verify_connectivity
from axon.v2.api import router as v2_router
from axon.v2.store import make_store

logger = logging.getLogger(__name__)


def _prewarm_store(store: Any) -> None:
    """Hydrate the graph cache in the background. Failures are logged,
    never raised: endpoints will retry the load on demand."""
    try:
        snap = store.load()
        logger.info("graph prewarm done: %d nodes, %d edges", len(snap.nodes), len(snap.edges))
    except Exception:
        logger.exception("graph prewarm failed; first request will retry the load")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup + shutdown."""
    logger.info("axon API starting up...")

    connected = await verify_connectivity()
    if not connected:
        logger.warning("Neo4j not reachable at startup — some endpoints will be degraded")

    prewarm: asyncio.Task[None] | None = None
    if get_settings().axon_store == "neo4j" and connected:
        # Hydrating a large graph can take minutes and gigabytes. Do it
        # in the background at boot so the first UI request does not
        # pay for it synchronously and health stays responsive.
        prewarm = asyncio.create_task(asyncio.to_thread(_prewarm_store, app.state.v2_store))

    yield

    if prewarm is not None and not prewarm.done():
        prewarm.cancel()
    logger.info("axon API shutting down...")
    await close_drivers()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="axon",
        description="API-first code graph: per-API dependency views and NL ask",
        version="0.2.0",
        debug=settings.api_debug,
        lifespan=lifespan,
    )
    app.state.v2_store = make_store(settings.axon_store)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Tighten in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def dev_token_auth(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if settings.api_auth_mode.lower() != "dev":
            return await call_next(request)
        if request.url.path in {"/api/v1/health"} or request.url.path.startswith("/docs"):
            return await call_next(request)
        expected = f"Bearer {settings.api_dev_token}"
        if request.headers.get("authorization") != expected:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    # Clean break (v2): the Bazel-centric v1 ops/graph/breaking surfaces are
    # unmounted. Health stays at /api/v1/health for existing probes.
    app.include_router(health.router, prefix="/api/v1", tags=["health"])
    app.include_router(v2_router, prefix="/api/v2", tags=["v2"])

    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    uvicorn.run(
        "axon.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.api_debug,
    )


if __name__ == "__main__":
    main()
