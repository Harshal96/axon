from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def initialize_schema():
    """v2 unit tests use the in-memory store; shadow the Neo4j autouse."""
    yield


@pytest.fixture(autouse=True)
def clean_graph():
    yield


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "v2"


@pytest.fixture(scope="session")
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture()
def monorepo_snap(fixtures: Path):
    from axon.v2.indexer import index_repo

    snap, _ = index_repo(str(fixtures / "monorepo"))
    return snap
