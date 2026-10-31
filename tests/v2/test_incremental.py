import subprocess
from pathlib import Path

import pytest

from axon.v2.indexer import index_repo
from axon.v2.models import NodeKind
from axon.v2.store import MemoryStore


def git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def commit(root):
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    return git(root, "rev-parse", "HEAD")


def fixture_repo(root):
    git(root, "init")
    (root / "axon.yaml").write_text(
        "repo: sample\nservices: [{id: app, roots: [app]}, {id: remote, roots: [remote]}]\n"
    )
    for owner in ["app", "remote"]:
        (root / owner).mkdir()
        for i in range(10):
            (root / owner / f"module{i}.py").write_text(f"def helper{i}(): pass\n")
    (root / "app/main.py").write_text(
        """import requests
from fastapi import FastAPI
app=FastAPI()
@app.get('/local')
def run(): requests.get('/remote')
"""
    )
    (root / "remote/main.py").write_text(
        "from fastapi import FastAPI\napp=FastAPI()\n@app.get('/remote')\ndef remote(): pass\n"
    )
    return commit(root)


def canonical(snapshot):
    data = snapshot.model_dump(mode="json")
    for node in data["nodes"].values():
        node["props"].pop("index_generation", None)
    data["edges"] = sorted(data["edges"], key=lambda edge: str(sorted(edge.items())))
    data["unresolved"] = sorted(data["unresolved"])
    data["diagnostics"] = sorted(data["diagnostics"])
    return data


def test_incremental_body_edit_equals_full_rebuild(tmp_path):
    from axon.v2.incremental import index_and_publish

    root = tmp_path / "repo"
    root.mkdir()
    first = fixture_repo(root)
    store = MemoryStore()
    index_and_publish(root, store, commit=first, state_dir=tmp_path / "state")
    old_reader = store.load()
    (root / "app/main.py").write_text(
        (root / "app/main.py")
        .read_text()
        .replace("requests.get('/remote')", "requests.get('/new')")
    )
    second = commit(root)
    snap, _, report = index_and_publish(root, store, commit=second, state_dir=tmp_path / "state")
    full, _ = index_repo(root, commit=second, use_cache=False, workers=1)
    assert report["mode"] == "incremental"
    assert report["files_reused"] > 0
    assert canonical(snap) == canonical(full)
    assert store.load() is not old_reader
    old_api = next(
        node for node in old_reader.by_kind(NodeKind.API_ENDPOINT) if node.props["path"] == "/local"
    )
    assert old_reader.outbound_calls(old_api.id)[0][1].service_id == "remote"
    assert store.load().outbound_calls(old_api.id)[0][1].kind == NodeKind.EXTERNAL_API


def test_incremental_endpoint_rename_relinks_unchanged_callers(tmp_path):
    from axon.v2.incremental import index_and_publish

    root = tmp_path / "repo"
    root.mkdir()
    first = fixture_repo(root)
    store = MemoryStore()
    index_and_publish(root, store, commit=first, state_dir=tmp_path / "state")
    path = root / "remote/main.py"
    path.write_text(path.read_text().replace("'/remote'", "'/different'"))
    second = commit(root)
    snap, _, report = index_and_publish(root, store, commit=second, state_dir=tmp_path / "state")
    assert report["mode"] == "incremental"
    full, _ = index_repo(root, commit=second, use_cache=False, workers=1)
    assert canonical(snap) == canonical(full)


def test_changed_configuration_forces_full_rebuild(tmp_path):
    from axon.v2.incremental import index_and_publish

    root = tmp_path / "repo"
    root.mkdir()
    first = fixture_repo(root)
    store = MemoryStore()
    index_and_publish(root, store, commit=first, state_dir=tmp_path / "state")
    config = root / "axon.yaml"
    config.write_text(config.read_text() + "ignore: [noise]\n")
    second = commit(root)
    _, _, report = index_and_publish(root, store, commit=second, state_dir=tmp_path / "state")
    assert report["mode"] == "full"
    assert report["fallback_reason"] == "config-changed"


def test_commit_moving_during_index_never_publishes(tmp_path, monkeypatch):
    import pytest

    import axon.v2.incremental as incremental

    root = tmp_path / "repo"
    root.mkdir()
    first = fixture_repo(root)
    store = MemoryStore()
    incremental.index_and_publish(root, store, commit=first, state_dir=tmp_path / "state")
    before = store.load()
    original = incremental.index_repo

    def moving(*args, **kwargs):
        result = original(*args, **kwargs)
        path = root / "app/module0.py"
        path.write_text("def changed(): pass\n")
        commit(root)
        return result

    monkeypatch.setattr(incremental, "index_repo", moving)
    with pytest.raises(RuntimeError, match="checkout"):
        incremental.index_and_publish(
            root, store, commit=first, force=True, state_dir=tmp_path / "state"
        )
    assert store.load() is before


def test_stale_requested_commit_does_not_reuse_facts(tmp_path):
    from axon.v2.incremental import index_and_publish

    root = tmp_path / "repo"
    root.mkdir()
    first = fixture_repo(root)
    store = MemoryStore()
    index_and_publish(root, store, commit=first, state_dir=tmp_path / "state")
    path = root / "app/module0.py"
    path.write_text("def changed(): pass\n")
    commit(root)
    _, _, report = index_and_publish(root, store, commit=first, state_dir=tmp_path / "state")
    assert report["mode"] == "full"
    assert report["fallback_reason"] == "commit-checkout-mismatch"


def test_golden_failure_keeps_previous_generation(tmp_path):
    import pytest

    from axon.v2.incremental import index_and_publish

    root = tmp_path / "repo"
    root.mkdir()
    fixture_repo(root)
    (root / "axon.golden.yaml").write_text(
        """version: 1
cases:
  - api: {service: app, method: GET, path: /local}
    expect: [{service: remote, path: /remote}]
"""
    )
    first = commit(root)
    store = MemoryStore()
    index_and_publish(root, store, commit=first, state_dir=tmp_path / "state")
    before = store.load()
    path = root / "app/main.py"
    path.write_text(path.read_text().replace("requests.get('/remote')", "requests.get('/missing')"))
    second = commit(root)
    with pytest.raises(ValueError, match="golden verification failed"):
        index_and_publish(root, store, commit=second, state_dir=tmp_path / "state")
    assert store.load() is before


@pytest.mark.parametrize("edit", ["symbol", "delete", "move", "endpoint", "spec", "build"])
def test_scripted_edits_equal_full_rebuild(tmp_path, edit):
    from axon.v2.incremental import index_and_publish

    root = tmp_path / "repo"
    root.mkdir()
    first = fixture_repo(root)
    store = MemoryStore()
    index_and_publish(root, store, commit=first, state_dir=tmp_path / "state")
    if edit == "symbol":
        (root / "app/module0.py").write_text("def renamed(): pass\n")
    elif edit == "delete":
        (root / "app/module0.py").unlink()
    elif edit == "move":
        (root / "app/module0.py").rename(root / "app/moved.py")
    elif edit == "endpoint":
        path = root / "app/main.py"
        path.write_text(path.read_text() + "@app.get('/remote')\ndef collision(): pass\n")
    elif edit == "spec":
        (root / "remote/openapi.yaml").write_text(
            """openapi: 3.0.0
info: {title: Example, version: '1'}
paths: {/extra: {get: {operationId: extra, responses: {}}}}
"""
        )
    else:
        (root / "remote/BUILD").write_text(
            "swagger_client(name='sdk', python_wheel_name='generated_sdk')\n"
        )
    second = commit(root)
    snap, _, report = index_and_publish(root, store, commit=second, state_dir=tmp_path / "state")
    full, _ = index_repo(root, commit=second, use_cache=False, workers=1)
    assert canonical(snap) == canonical(full)
    assert report["mode"] == ("full" if edit in {"spec", "build"} else "incremental")


@pytest.mark.parametrize(
    "case_root",
    sorted(
        p for p in (Path(__file__).resolve().parents[1] / "fixtures/evals").iterdir() if p.is_dir()
    ),
    ids=lambda p: p.name,
)
def test_eval_fixture_update_matches_full_rebuild(tmp_path, case_root):
    import shutil

    from axon.v2.incremental import index_and_publish

    root = tmp_path / "repo"
    shutil.copytree(case_root, root)
    git(root, "init")
    first = commit(root)
    store = MemoryStore()
    index_and_publish(root, store, commit=first, state_dir=tmp_path / "state")
    candidates = (
        sorted(root.rglob("*.py")) + sorted(root.rglob("*.java")) + sorted(root.rglob("*.go"))
    )
    path = candidates[0] if candidates else root / "axon.yaml"
    marker = (
        "# differential edit\n" if path.suffix in {".py", ".yaml"} else "// differential edit\n"
    )
    path.write_text(marker + path.read_text())
    second = commit(root)
    snap, _, _ = index_and_publish(root, store, commit=second, state_dir=tmp_path / "state")
    full, _ = index_repo(root, commit=second, use_cache=False, workers=1)
    assert canonical(snap) == canonical(full)


def test_shadow_audit_is_read_only_and_detects_drift(tmp_path):
    from axon.v2.incremental import audit_state, index_and_publish
    from axon.v2.indexstate import StateStore

    root = tmp_path / "repo"
    root.mkdir()
    revision = fixture_repo(root)
    store = MemoryStore()
    index_and_publish(root, store, commit=revision, state_dir=tmp_path / "state")
    state_store = StateStore("sample", tmp_path / "state")
    original = state_store.path.read_bytes()
    assert audit_state(root, tmp_path / "state", snapshot=store.load())["ok"]
    assert state_store.path.read_bytes() == original
    store.load().edges.clear()
    assert not audit_state(root, tmp_path / "state", snapshot=store.load())["ok"]


def test_state_sqlite_failure_is_warning_after_graph_publication(tmp_path, monkeypatch):
    import sqlite3

    from axon.v2.incremental import index_and_publish
    from axon.v2.indexstate import StateStore

    root = tmp_path / "repo"
    root.mkdir()
    revision = fixture_repo(root)
    store = MemoryStore()

    def fail(self, state):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(StateStore, "save", fail)
    phases = []
    snap, _, report = index_and_publish(
        root,
        store,
        commit=revision,
        state_dir=tmp_path / "state",
        progress=lambda phase, done, total: phases.append(phase),
    )
    assert not report["state_persisted"]
    assert report["warnings"] == ["incremental state not saved: disk full"]
    assert snap.nodes
    assert phases.index("publish") < phases.index("state") < phases.index("done")
    assert phases[-1] == "done"


@pytest.mark.parametrize("degraded", [False, True])
def test_parse_diagnostics_do_not_disable_incremental_and_degraded_retries(tmp_path, degraded):
    from axon.v2.incremental import index_and_publish
    from axon.v2.indexer import IndexTimings

    root = tmp_path / "repo"
    root.mkdir()
    fixture_repo(root)
    source = root / "app/module0.py"
    source.write_text("def broken(:\n" if degraded else "value = '\\q'\n")
    if degraded:
        # Retry scheduling is not a Git change and must not trip the 15% guard.
        for path in root.glob("*/module*.py"):
            path.write_text("def broken(:\n")
    first = commit(root)
    store = MemoryStore()
    index_and_publish(root, store, commit=first, state_dir=tmp_path / "state")
    timings = IndexTimings()
    snap, _, report = index_and_publish(
        root, store, commit=first, state_dir=tmp_path / "state", timings=timings
    )
    assert report["mode"] == "incremental"
    assert report["fallback_reason"] is None
    full, _ = index_repo(root, commit=first, use_cache=False, workers=1)
    assert canonical(snap) == canonical(full)
    if degraded:
        assert timings.files_parsed >= 1


def test_same_commit_skip_requires_current_build(tmp_path):
    from axon.v2.api import _already_indexed
    from axon.v2.models import repo_node_id

    root = tmp_path / "repo"
    root.mkdir()
    revision = fixture_repo(root)
    snap, _ = index_repo(root, commit=revision, use_cache=False, workers=1)
    store = MemoryStore()
    store.save(snap)
    assert _already_indexed(store, root, revision) is not None
    snap.nodes[repo_node_id("sample")].props["axon_build"] = "old-build"
    assert _already_indexed(store, root, revision) is None
