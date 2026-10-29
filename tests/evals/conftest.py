"""Keep the static v2 evals independent of the legacy Neo4j fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def initialize_schema() -> Iterator[None]:
    """Shadow the root Neo4j schema fixture for in-memory v2 evals."""
    yield


@pytest.fixture(autouse=True)
def clean_graph() -> Iterator[None]:
    """Shadow the root Neo4j cleanup fixture for in-memory v2 evals."""
    yield
