import os
import time

import httpx
import pytest
from neo4j import GraphDatabase

# ── Environment detection ─────────────────────────────────────────────────────

def _neo4j_uri() -> str:
    return os.getenv("TEST_NEO4J_URI", "bolt://localhost:7688")

def _api_base() -> str:
    return os.getenv("TEST_API_URL", "http://localhost:8081")

def _api_token() -> str:
    return os.getenv("TEST_API_TOKEN", "test-token")


def _configure_app_db_env() -> None:
    os.environ["NEO4J_URI"] = _neo4j_uri()
    os.environ.setdefault("NEO4J_USERNAME", "neo4j")
    os.environ.setdefault("NEO4J_PASSWORD", os.getenv("TEST_NEO4J_PASSWORD", "test-password"))
    from axon.config import get_settings

    get_settings.cache_clear()


# ── Neo4j session fixture ─────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def neo4j_driver():
    """Session-scoped Neo4j driver. Waits for Neo4j to be healthy."""
    _configure_app_db_env()
    uri = _neo4j_uri()
    driver = GraphDatabase.driver(uri, auth=("neo4j", "test-password"))
    # Wait up to 60s for Neo4j to accept connections
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            driver.verify_connectivity()
            break
        except Exception:
            time.sleep(2)
    else:
        pytest.fail(f"Neo4j not reachable at {uri} after 60s")
    yield driver
    driver.close()


@pytest.fixture(scope="session", autouse=True)
def initialize_schema(neo4j_driver):
    """Initialize Neo4j schema once per test session."""
    from axon.db.init import initialize_schema
    initialize_schema()


@pytest.fixture(autouse=True)
def clean_graph(neo4j_driver, request):
    """
    Wipe the graph before each test that requests it.
    Tests marked with @pytest.mark.no_clean skip the wipe.
    Tier2+ tests manage their own cleanup via session-scoped fixtures.
    """
    if "no_clean" in request.keywords or "tier2" in request.keywords \
            or "tier3" in request.keywords:
        yield
        return
    from axon.api.cache import _MOCK_CACHE

    _MOCK_CACHE.clear()
    with neo4j_driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
    yield


# ── HTTP client fixture ───────────────────────────────────────────────────────

@pytest.fixture
def api_client() -> httpx.Client:
    return httpx.Client(
        base_url=_api_base(),
        headers={"Authorization": f"Bearer {_api_token()}"},
        timeout=30.0,
    )


@pytest.fixture
async def async_api_client() -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        base_url=_api_base(),
        headers={"Authorization": f"Bearer {_api_token()}"},
        timeout=30.0,
    ) as client:
        yield client


# ── Workspace fixture (Tier 2+) ───────────────────────────────────────────────

@pytest.fixture(scope="session")
def monorepo_root() -> str:
    """Absolute path to the synthetic test monorepo."""
    return os.path.join(os.path.dirname(__file__), "fixtures", "monorepo")

pytest_plugins = ["tests.fixtures.seed_graph", "tests.fixtures.history_seed"]
