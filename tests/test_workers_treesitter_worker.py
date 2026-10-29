"""
Integration test for TreeSitterWorker.
Uses fixture Python files and a mocked Neo4j query for targets.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from axon.workers.treesitter.worker import TreeSitterWorker

FIXTURES = Path(__file__).parent / "fixtures"

# Fake Neo4j response: a single Python target
MOCK_TARGETS = [
    {
        "id": "target:://services/payment:payment_lib",
        "label": "//services/payment:payment_lib",
        "package": "//fixtures/sample_python",  # points at our fixture dir
    },
]


@pytest.fixture(autouse=True)
def mock_neo4j_query():
    """Mock the _get_python_targets method so no Neo4j connection is needed."""
    with patch.object(TreeSitterWorker, "_get_python_targets", return_value=MOCK_TARGETS):
        yield


@pytest.fixture(autouse=True)
def mock_writers():
    """Mock all writer functions — we test parsing logic, not Neo4j writes."""
    with patch("axon.workers.treesitter.worker.writers") as mock_w:
        mock_w.write_source_files.return_value = 2
        mock_w.write_builds_edges.return_value = 2
        mock_w.write_symbols.return_value = 10
        mock_w.write_defines_edges.return_value = 10
        mock_w.write_imports_edges.return_value = 1
        mock_w.write_calls_edges.return_value = 3
        yield mock_w


def test_worker_runs_successfully() -> None:
    # workspace_root = tests/ directory (parent of fixtures/)
    worker = TreeSitterWorker(
        workspace_root=str(FIXTURES.parent),
        commit_sha="test-sha-ts-001",
    )
    result = worker.run()
    assert result.success is True
    assert result.nodes_written > 0
    assert result.edges_written > 0
    assert len(result.errors) == 0


def test_worker_calls_write_source_files(mock_writers) -> None:
    worker = TreeSitterWorker(
        workspace_root=str(FIXTURES.parent),
        commit_sha="test-sha-ts-002",
    )
    worker.run()
    mock_writers.write_source_files.assert_called_once()
    file_dicts = mock_writers.write_source_files.call_args[0][0]
    paths = [f["path"] for f in file_dicts]
    assert any("payment.py" in p for p in paths)
    assert any("checkout.py" in p for p in paths)


def test_worker_calls_write_symbols(mock_writers) -> None:
    worker = TreeSitterWorker(
        workspace_root=str(FIXTURES.parent),
        commit_sha="test-sha-ts-003",
    )
    worker.run()
    mock_writers.write_symbols.assert_called_once()
    symbol_dicts = mock_writers.write_symbols.call_args[0][0]
    names = {s["name"] for s in symbol_dicts}
    assert "PaymentService" in names
    assert "charge" in names
    assert "CheckoutService" in names


def test_worker_calls_write_builds_edges(mock_writers) -> None:
    worker = TreeSitterWorker(
        workspace_root=str(FIXTURES.parent),
        commit_sha="test-sha-ts-004",
    )
    worker.run()
    mock_writers.write_builds_edges.assert_called_once()
    edges = mock_writers.write_builds_edges.call_args[0][0]
    # All edges should reference the mock target
    for edge in edges:
        assert edge["from_id"] == "target:://services/payment:payment_lib"


def test_worker_calls_write_defines_edges(mock_writers) -> None:
    worker = TreeSitterWorker(
        workspace_root=str(FIXTURES.parent),
        commit_sha="test-sha-ts-005",
    )
    worker.run()
    mock_writers.write_defines_edges.assert_called_once()
    edges = mock_writers.write_defines_edges.call_args[0][0]
    assert len(edges) > 0
    for edge in edges:
        assert edge["from_id"].startswith("file::")
        assert edge["to_id"].startswith("symbol::")


def test_worker_result_counts(mock_writers) -> None:
    worker = TreeSitterWorker(
        workspace_root=str(FIXTURES.parent),
        commit_sha="test-sha-ts-006",
    )
    result = worker.run()
    # 2 files + 10 symbols = 12 nodes
    assert result.nodes_written == 12
    # 2 builds + 10 defines + 1 imports + 3 calls = 16 edges
    assert result.edges_written == 16
