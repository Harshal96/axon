"""Tests for SwaggerWorker orchestration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from axon.workers.swagger.worker import SwaggerWorker

FIXTURES = Path(__file__).parent / "fixtures"

MOCK_TARGETS = [
    {
        "id": "target:://services/payment:generate_payment_client_py",
        "label": "//services/payment:generate_payment_client_py",
        "genrule_srcs": ["//services/payment:swagger.json"],
        "genrule_outs": ["payment_client.py"],
    }
]


@pytest.fixture(autouse=True)
def mock_neo4j():
    with patch.object(SwaggerWorker, "_get_swagger_targets", return_value=MOCK_TARGETS):
        yield


@pytest.fixture(autouse=True)
def mock_swagger_file():
    """Make the swagger file findable from the workspace root."""
    with patch("axon.workers.swagger.worker.parse_swagger_file") as mock_parse:
        from axon.workers.swagger.parser import parse_swagger_file as real_parse

        mock_parse.side_effect = lambda path, root: real_parse(
            "fixtures/sample.swagger.json", str(FIXTURES.parent)
        )
        yield


@pytest.fixture(autouse=True)
def mock_writers():
    with patch("axon.workers.swagger.worker.writers") as mock_w:
        mock_w.write_swagger_schemas.return_value = 1
        mock_w.write_api_endpoints.return_value = 3
        mock_w.write_generated_clients.return_value = 1
        mock_w.write_defines_endpoint_edges.return_value = 3
        mock_w.write_generated_from_edges.return_value = 1
        yield mock_w


def test_swagger_worker_success() -> None:
    worker = SwaggerWorker(workspace_root=str(FIXTURES.parent), commit_sha="test-sha")
    result = worker.run()
    assert result.success is True
    assert len(result.errors) == 0


def test_swagger_worker_node_count(mock_writers) -> None:
    worker = SwaggerWorker(workspace_root=str(FIXTURES.parent), commit_sha="test-sha")
    result = worker.run()
    # 1 schema + 3 endpoints + 1 client = 5
    assert result.nodes_written == 5


def test_swagger_worker_edge_count(mock_writers) -> None:
    worker = SwaggerWorker(workspace_root=str(FIXTURES.parent), commit_sha="test-sha")
    result = worker.run()
    # 3 defines_endpoint + 1 generated_from = 4
    assert result.edges_written == 4


def test_swagger_worker_writes_endpoints(mock_writers) -> None:
    worker = SwaggerWorker(workspace_root=str(FIXTURES.parent), commit_sha="test-sha")
    worker.run()
    mock_writers.write_api_endpoints.assert_called_once()
    endpoints = mock_writers.write_api_endpoints.call_args[0][0]
    methods = {ep["http_method"] for ep in endpoints}
    assert "POST" in methods
    assert "GET" in methods
