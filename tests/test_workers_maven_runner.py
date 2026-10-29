from __future__ import annotations

from unittest.mock import MagicMock, patch

from axon.workers.java.maven.runner import list_transitive_deps


@patch("axon.workers.java.maven.runner.subprocess.run")
def test_list_transitive_deps_success(mock_run):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="[INFO]    org.apache.commons:commons-collections:jar:3.2.1:compile\n"
        "[INFO]    com.google.guava:guava:jar:30.1.1-jre:compile\n",
    )

    deps = list_transitive_deps("pom.xml", "/tmp")
    assert len(deps) == 2
    assert deps[0].group_id == "org.apache.commons"
    assert deps[0].artifact_id == "commons-collections"
    assert deps[0].version == "3.2.1"
    assert deps[0].scope == "compile"


@patch("axon.workers.java.maven.runner.subprocess.run")
def test_list_transitive_deps_fallback(mock_run):

    mock_run.side_effect = FileNotFoundError()

    deps = list_transitive_deps("pom.xml", "/tmp")
    assert deps == []
