"""Tests for MavenWorker orchestration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from axon.workers.maven.worker import MavenWorker

FIXTURES = Path(__file__).parent / "fixtures"

MOCK_TARGETS = [
    {
        "id": "target:://services/payment:payment_gateway_java",
        "label": "//services/payment:payment_gateway_java",
        "genrule_srcs": ["//services/payment:pom.xml"],
    }
]


@pytest.fixture(autouse=True)
def mock_neo4j():
    with patch.object(MavenWorker, "_get_maven_targets", return_value=MOCK_TARGETS):
        yield


@pytest.fixture(autouse=True)
def mock_pom():
    with patch("axon.workers.maven.worker.parse_pom_file") as mock_parse:
        from axon.workers.maven.parser import parse_pom_file as real_parse

        mock_parse.side_effect = lambda path, root: real_parse(
            "fixtures/sample.pom.xml", str(FIXTURES.parent)
        )
        yield


@pytest.fixture(autouse=True)
def mock_writers():
    with patch("axon.workers.maven.worker.writers") as mock_w:
        mock_w.write_maven_artifacts.return_value = 3
        mock_w.write_maven_dep_edges.return_value = 3
        yield mock_w


def test_maven_worker_success() -> None:
    worker = MavenWorker(workspace_root=str(FIXTURES.parent), commit_sha="test-sha")
    result = worker.run()
    assert result.success is True


def test_maven_worker_node_count(mock_writers) -> None:
    worker = MavenWorker(workspace_root=str(FIXTURES.parent), commit_sha="test-sha")
    result = worker.run()
    assert result.nodes_written == 3


def test_maven_worker_edge_count(mock_writers) -> None:
    worker = MavenWorker(workspace_root=str(FIXTURES.parent), commit_sha="test-sha")
    result = worker.run()
    assert result.edges_written == 3


def test_maven_worker_writes_artifacts(mock_writers) -> None:
    worker = MavenWorker(workspace_root=str(FIXTURES.parent), commit_sha="test-sha")
    worker.run()
    mock_writers.write_maven_artifacts.assert_called_once()
    arts = mock_writers.write_maven_artifacts.call_args[0][0]
    ids = {a["id"] for a in arts}
    assert "maven::com.google.guava:guava:32.1.3-jre" in ids
