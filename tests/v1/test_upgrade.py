from __future__ import annotations

import json
from pathlib import Path

import pytest

from axon.v1.graph import GraphSnapshot
from axon.v1.models import GraphNode, NodeKind
from axon.v1.store import Neo4jStore, save_snapshot_file
from axon.v1.upgrade import compare_snapshots, inspect_snapshot, upgrade_check

COMPAT = Path(__file__).resolve().parents[1] / "fixtures" / "compat"


def _node(nid: str, repo: str = "r", **props: str) -> GraphNode:
    return GraphNode(id=nid, kind=NodeKind.REPO, name=nid, repo_id=repo, props=props)


def test_upgrade_check_snapshot_verdicts(tmp_path):
    assert inspect_snapshot(COMPAT / "snapshot-v1.json")["status"] == "compatible"
    assert inspect_snapshot(tmp_path / "nope.json")["status"] == "missing"
    data = json.loads((COMPAT / "snapshot-v1.json").read_text())
    data["schema_version"] = 99
    target = tmp_path / "new.json"
    target.write_text(json.dumps(data))
    report = upgrade_check(snapshot=target)
    assert report["verdict"] == "blocked"
    assert report["checks"]["snapshot"]["status"] == "too-new"


def test_upgrade_check_repo_and_state(tmp_path):
    report = upgrade_check(repo="tests/fixtures/v1/monorepo", state_dir=tmp_path)
    assert report["checks"]["service_map"]["status"] == "compatible"
    assert report["checks"]["state"]["status"] == "missing"
    assert report["verdict"] == "compatible"


def test_compare_snapshots_reports_endpoint_loss():
    before = GraphSnapshot()
    before.add_node(GraphNode(id="ep1", kind=NodeKind.API_ENDPOINT, name="ep1", repo_id="r"))
    after = GraphSnapshot()
    report = compare_snapshots(before, after)
    assert report["endpoints_removed"] == ["ep1"]
    assert report["nodes_before"] == 1 and report["nodes_after"] == 0


def test_rollback_without_previous_generation_errors(monkeypatch):
    from tests.v1.test_store import _FakeSession

    store = Neo4jStore()
    session = _FakeSession()
    monkeypatch.setattr(store, "_session", lambda: session)
    with pytest.raises(ValueError, match="no previous generation"):
        store.rollback_generation("r")


def test_rollback_flips_to_previous_generation(monkeypatch):
    from tests.v1.test_store import _FakeResult, _FakeSession

    class PubSession(_FakeSession):
        def run(self, query, **kwargs):
            if query.startswith("MATCH (p:_AxonPublication)"):
                return _FakeResult(
                    [
                        {
                            "repo_id": "r",
                            "generation": "g2",
                            "prev_generation": "g1",
                            "schema_version": 1,
                        }
                    ]
                )
            return super().run(query, **kwargs)

    store = Neo4jStore()
    session = PubSession()
    monkeypatch.setattr(store, "_session", lambda: session)
    rows = store.rollback_generation("r")
    assert rows == [{"repo_id": "r", "generation": "g1", "prev_generation": "g2"}]
    assert store._generations["r"] == "g1"


def test_safety_snapshot_written_on_build_change(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_STATE_DIR", str(tmp_path))
    store = Neo4jStore()
    current = GraphSnapshot(nodes={"repo::r": _node("repo::r", axon_build="old")})
    incoming = GraphSnapshot(nodes={"repo::r": _node("repo::r", axon_build="new")})
    store._write_safety_snapshot(current, incoming)
    assert store.last_safety_snapshot
    saved = Path(store.last_safety_snapshot)
    assert saved.is_file()
    assert "repo::r" in saved.read_text()
    # No build change: no second snapshot.
    store.last_safety_snapshot = ""
    store._write_safety_snapshot(incoming, incoming)
    assert store.last_safety_snapshot == ""


def test_publication_retains_previous_generation(monkeypatch):
    from tests.v1.test_store import _FakeResult, _FakeSession

    class PubSession(_FakeSession):
        def run(self, query, **kwargs):
            if query.startswith("MATCH (p:_AxonPublication)"):
                return _FakeResult(
                    [
                        {
                            "repo_id": "r",
                            "generation": "g1",
                            "prev_generation": None,
                            "schema_version": 1,
                        }
                    ]
                )
            return super().run(query, **kwargs)

    store = Neo4jStore()
    session = PubSession()
    monkeypatch.setattr(store, "_session", lambda: session)
    snap = GraphSnapshot(
        nodes={"a": GraphNode(id="a", kind=NodeKind.SYMBOL, name="a", repo_id="r")}
    )
    store.save(snap)
    pub_calls = [(q, k) for q, k in session.calls if "MERGE (p:_AxonPublication" in q]
    assert pub_calls
    row = pub_calls[0][1]["rows"][0]
    assert row["prev_generation"] == "g1"
    assert row["schema_version"] == 1
    deletes = [(q, k) for q, k in session.calls if "DETACH DELETE" in q]
    assert deletes
    assert any("g1" in k.get("keep_generations", []) for _, k in deletes)


def test_snapshot_import_loads_through_gate(tmp_path):
    snap = GraphSnapshot(
        nodes={"a": GraphNode(id="a", kind=NodeKind.SYMBOL, name="a", repo_id="r")}
    )
    target = tmp_path / "snap.json"
    save_snapshot_file(snap, target)
    from axon.v1.store import load_snapshot_file

    assert load_snapshot_file(target).nodes["a"].name == "a"
