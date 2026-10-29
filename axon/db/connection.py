from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager

from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncSession, Driver, GraphDatabase, Session

from axon.config import get_settings

logger = logging.getLogger(__name__)

_async_driver: AsyncDriver | None = None
_sync_driver: Driver | None = None


def get_sync_driver() -> Driver:
    """Returns the singleton synchronous Neo4j driver. Creates it on first call."""
    global _sync_driver
    if _sync_driver is None:
        settings = get_settings()
        _sync_driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
            max_connection_pool_size=settings.neo4j_max_connection_pool_size,
            connection_timeout=settings.neo4j_connection_timeout_seconds,
        )
        logger.info("Sync Neo4j driver created (uri=%s)", settings.neo4j_uri)
    return _sync_driver


async def get_async_driver() -> AsyncDriver:
    """Returns the singleton asynchronous Neo4j driver."""
    global _async_driver
    if _async_driver is None:
        settings = get_settings()
        _async_driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
            max_connection_pool_size=settings.neo4j_max_connection_pool_size,
            connection_timeout=settings.neo4j_connection_timeout_seconds,
        )
        logger.info("Async Neo4j driver created (uri=%s)", settings.neo4j_uri)
    return _async_driver


@contextmanager
def sync_session() -> Generator[Session, None, None]:
    """Context manager for a synchronous Neo4j session."""
    settings = get_settings()
    driver = get_sync_driver()
    with driver.session(database=settings.neo4j_database) as session:
        yield session


@asynccontextmanager
async def async_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager for an async Neo4j session."""
    settings = get_settings()
    driver = await get_async_driver()
    async with driver.session(database=settings.neo4j_database) as session:
        yield session


async def verify_connectivity() -> bool:
    """Returns True if Neo4j is reachable, False otherwise."""
    try:
        driver = await get_async_driver()
        await driver.verify_connectivity()
        return True
    except Exception as e:
        logger.warning("Neo4j connectivity check failed: %s", e)
        return False


async def close_drivers() -> None:
    """Gracefully close all drivers. Call on application shutdown."""
    global _async_driver, _sync_driver
    if _async_driver:
        await _async_driver.close()
        _async_driver = None
    if _sync_driver:
        _sync_driver.close()
        _sync_driver = None
    logger.info("Neo4j drivers closed")
