from pathlib import Path

import pytest

from axon.v1.graph import GraphSnapshot
from axon.v1.scoped_index import focused_result, scoped_paths
from axon.v1.servicemap import load_service_map


def test_scoped_paths(tmp_path: Path):
    (tmp_path / "axon.yaml").write_text(
        "repo: demo\nservices: [{id: a, roots: [a]}, {id: b, roots: [b]}]\n"
    )
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "app.py").write_text("def hello(): pass\n")
    sm = load_service_map(tmp_path)
    assert scoped_paths(tmp_path, sm, ["a", "a"]) == {"a/app.py"}
    with pytest.raises(ValueError, match="unknown services"):
        scoped_paths(tmp_path, sm, ["missing"])


def test_focused_report_empty_delta():
    report = focused_result(GraphSnapshot(), GraphSnapshot(), "demo", ["a"])
    assert report["services"] == ["a"]
    assert report["graph_delta"] == {"added": [], "removed": []}
    assert report["coverage"] == {}


def test_scoped_publish_forces_fresh_parse_and_keeps_other_service(tmp_path, monkeypatch):
    from axon.v1 import indexer
    from axon.v1.incremental import index_and_publish
    from axon.v1.models import NodeKind
    from axon.v1.store import MemoryStore

    (tmp_path / "axon.yaml").write_text(
        "repo: demo\nservices: [{id: a, roots: [a]}, {id: b, roots: [b]}]\n"
    )
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "app.py").write_text(
            "from fastapi import FastAPI\napp=FastAPI()\n@app.get('/"
            + name
            + "')\ndef hello(): pass\n"
        )
    monkeypatch.setenv("AXON_FACTS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AXON_INDEX_WORKERS", "1")
    store = MemoryStore()
    index_and_publish(tmp_path, store, state_dir=tmp_path / "state")
    original = indexer._parse_one
    parsed = []

    def parse(task):
        parsed.append(task)
        return original(task)

    monkeypatch.setattr(indexer, "_parse_one", parse)
    snapshot, _, report = index_and_publish(
        tmp_path, store, state_dir=tmp_path / "state", services=["a"]
    )
    assert any(task[1] == "a/app.py" and task[2] == "" for task in parsed)
    assert {node.service_id for node in snapshot.by_kind(NodeKind.API_ENDPOINT)} == {"a", "b"}
    assert set(report["coverage"]) == {"a"}
    assert report["graph_delta"] == {"added": [], "removed": []}


def test_scoped_api_report_and_unknown_service(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from axon.api.app import create_app

    (tmp_path / "axon.yaml").write_text("repo: demo\nservices: [{id: a, roots: [a]}]\n")
    (tmp_path / "a").mkdir()
    (tmp_path / "a/main.py").write_text("def hello(): pass\n")
    monkeypatch.setenv("AXON_FACTS_CACHE_DIR", str(tmp_path / "cache"))
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/index", json={"repo_path": str(tmp_path), "services": ["a"]}
        )
        assert response.status_code == 200, response.text
        assert response.json()["services"] == ["a"]
        assert "graph_delta" in response.json()
        response = client.post(
            "/api/v1/index/jobs", json={"repo_path": str(tmp_path), "services": ["missing"]}
        )
        assert response.status_code == 400


def test_local_cli_scoped_json(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    from cli.app import app

    (tmp_path / "axon.yaml").write_text("repo: demo\nservices: [{id: a, roots: [a]}]\n")
    (tmp_path / "a").mkdir()
    (tmp_path / "a/main.py").write_text("def hello(): pass\n")
    monkeypatch.setenv("AXON_FACTS_CACHE_DIR", str(tmp_path / "cache"))
    result = CliRunner().invoke(
        app,
        ["index", "--repo", str(tmp_path), "--local", "--service", "a", "--service", "a", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["services"] == ["a"]
