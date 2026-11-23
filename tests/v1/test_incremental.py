import subprocess
from pathlib import Path

import pytest

from axon.v1.indexer import index_repo
from axon.v1.models import NodeKind
from axon.v1.store import MemoryStore


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
    data.pop("publication_id", None)
    data.pop("publication_stats", None)
    data.pop("link_reports", None)
    for inputs in data.get("link_inputs", {}).values():
        inputs["source_generation"] = ""
    for node in data["nodes"].values():
        node["props"].pop("index_generation", None)
    data["edges"] = sorted(data["edges"], key=lambda edge: str(sorted(edge.items())))
    data["unresolved"] = sorted(data["unresolved"])
    data["diagnostics"] = sorted(data["diagnostics"])
    return data


def test_incremental_body_edit_equals_full_rebuild(tmp_path):
    from axon.v1.incremental import index_and_publish

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
    from axon.v1.incremental import index_and_publish

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
    from axon.v1.incremental import index_and_publish

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

    import axon.v1.incremental as incremental

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


def test_stale_requested_commit_preserves_prior_publication(tmp_path):
    from axon.v1.incremental import index_and_publish

    root = tmp_path / "repo"
    root.mkdir()
    first = fixture_repo(root)
    store = MemoryStore()
    index_and_publish(root, store, commit=first, state_dir=tmp_path / "state")
    before = store.load()
    path = root / "app/module0.py"
    path.write_text("def changed(): pass\n")
    commit(root)
    with pytest.raises(ValueError, match="does not match checkout HEAD"):
        index_and_publish(root, store, commit=first, state_dir=tmp_path / "state")
    assert store.load() is before


def test_golden_failure_keeps_previous_generation(tmp_path):
    import pytest

    from axon.v1.incremental import index_and_publish

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
    from axon.v1.incremental import index_and_publish

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

    from axon.v1.incremental import index_and_publish

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
    from axon.v1.incremental import audit_state, index_and_publish
    from axon.v1.indexstate import StateStore

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

    from axon.v1.incremental import index_and_publish
    from axon.v1.indexstate import StateStore

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
    from axon.v1.incremental import index_and_publish
    from axon.v1.indexer import IndexTimings

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
    from axon.v1.api import _already_indexed
    from axon.v1.models import repo_node_id

    root = tmp_path / "repo"
    root.mkdir()
    revision = fixture_repo(root)
    snap, _ = index_repo(root, commit=revision, use_cache=False, workers=1)
    store = MemoryStore()
    store.save(snap)
    assert _already_indexed(store, root, revision) is not None
    snap.nodes[repo_node_id("sample")].props["axon_build"] = "old-build"
    assert _already_indexed(store, root, revision) is None


def remote_repos(tmp_path):
    import yaml

    roots = []
    for repo, service, route in [("A", "orders", "/orders"), ("B", "ledger", "/entries")]:
        root = tmp_path / repo
        root.mkdir()
        git(root, "init")
        decl = {"id": service, "roots": [service]}
        if repo == "A":
            decl["calls"] = [
                {
                    "id": "ledger",
                    "from_api": "GET /orders",
                    "to": {"repo": "B", "service": "ledger", "method": "GET", "path": "/entries"},
                    "evidence": {"file": "orders/api.py", "line": 5, "text": "def handle(): pass"},
                }
            ]
        (root / "axon.yaml").write_text(yaml.safe_dump({"repo": repo, "services": [decl]}))
        (root / service).mkdir()
        (root / service / "api.py").write_text(
            "from fastapi import FastAPI\napp=FastAPI()\n\n@app.get('"
            + route
            + "')\ndef handle(): pass\n"
        )
        commit(root)
        roots.append(root)
    return roots


def index_remote(root, store, tmp_path, **kwargs):
    from axon.v1.incremental import index_and_publish

    return index_and_publish(
        root, store, commit=git(root, "rev-parse", "HEAD"), state_dir=tmp_path / "state", **kwargs
    )


@pytest.mark.parametrize("scope", [{}, {"services": ["orders"]}, {"api": "GET /orders"}])
def test_index_returns_committed_remote_projection_and_reports(tmp_path, scope):
    a, b = remote_repos(tmp_path)
    store = MemoryStore()
    index_remote(a, store, tmp_path)
    index_remote(b, store, tmp_path)
    snap, _, result = index_remote(a, store, tmp_path, **scope)
    edge = next(e for e in snap.edges if e.kind.value == "CALLS_API")
    assert snap.nodes[edge.to_id].repo_id == "B"
    assert snap.coverage_by_service("A")["orders"]["outbound_resolved"] == 1
    assert snap.unresolved == []
    assert result["publication_id"] == store.load().publication_id
    assert result["relinked_repos"] == ["A", "B"]
    assert result["link_inputs_missing"] == []
    assert result["link_sites_replayed"] == 0  # manual declarations are not prepared sites
    assert result["link_rows_staged"] >= 0
    assert result["link_reconcile_s"] >= 0
    snap.nodes[edge.to_id].props["path"] = "/mutated"
    assert store.load().nodes[edge.to_id].props["path"] == "/entries"


def test_parse_before_target_publication_reconciles_latest_catalog(tmp_path, monkeypatch):
    import axon.v1.incremental as incremental

    a, b = remote_repos(tmp_path)
    store = MemoryStore()
    original = incremental.index_repo

    def concurrent(root, **kwargs):
        result = original(root, **kwargs)
        if root == a:
            target, _ = original(b)
            store.publish_repository(target, repo_id="B")
        return result

    monkeypatch.setattr(incremental, "index_repo", concurrent)
    snap, _, _ = index_remote(a, store, tmp_path)
    edge = next(e for e in snap.edges if e.kind.value == "CALLS_API")
    assert snap.nodes[edge.to_id].repo_id == "B"


@pytest.mark.parametrize("present", [True, False])
def test_golden_validates_prospective_remote_endpoints(tmp_path, present):
    a, b = remote_repos(tmp_path)
    store = MemoryStore()
    if present:
        index_remote(b, store, tmp_path)
    (a / "axon.golden.yaml").write_text(
        "version: 1\ncases:\n  - api: {service: orders, method: GET, path: /orders}\n"
        "    expect: [{repo: B, service: ledger, method: GET, path: /entries}]\n"
    )
    commit(a)
    before = store.load()
    if present:
        snap, _, _ = index_remote(a, store, tmp_path)
        assert snap.coverage_by_service("A")["orders"]["outbound_resolved"] == 1
    else:
        with pytest.raises(ValueError, match="golden verification failed"):
            index_remote(a, store, tmp_path)
        assert store.load() is before


def test_source_path_conflict_and_explicit_reassignment(tmp_path):
    import shutil

    a, _ = remote_repos(tmp_path)
    other = tmp_path / "other"
    shutil.copytree(a, other)
    store = MemoryStore()
    index_remote(a, store, tmp_path)
    before = store.load()
    with pytest.raises(ValueError, match="source path"):
        index_remote(other, store, tmp_path)
    assert store.load() is before
    index_remote(other, store, tmp_path, replace_source=True)
    assert store.load().nodes["repo::A"].props["source_path"] == str(other)


def test_source_path_conflict_checked_again_at_commit(tmp_path, monkeypatch):
    import shutil

    import axon.v1.incremental as incremental

    a, _ = remote_repos(tmp_path)
    other = tmp_path / "other"
    shutil.copytree(a, other)
    store = MemoryStore()
    original = incremental.index_repo

    def concurrent(root, **kwargs):
        result = original(root, **kwargs)
        rival, _ = original(other)
        store.publish_repository(rival, repo_id="A")
        return result

    monkeypatch.setattr(incremental, "index_repo", concurrent)
    with pytest.raises(ValueError, match="source path"):
        index_remote(a, store, tmp_path)
    assert store.load().nodes["repo::A"].props["source_path"] == str(other)


def test_target_update_without_caller_checkout_and_structural_audit(tmp_path):
    import shutil

    from axon.v1.incremental import audit_state
    from axon.v1.store import load_snapshot_file, save_snapshot_file

    a, b = remote_repos(tmp_path)
    store = MemoryStore()
    index_remote(a, store, tmp_path)
    index_remote(b, store, tmp_path)
    assert audit_state(a, tmp_path / "state", snapshot=store.load())["ok"]
    retained = store.load().link_inputs["A"].source_generation
    path = tmp_path / "snapshot.json"
    save_snapshot_file(store.load(), path)
    shutil.rmtree(a)
    shutil.rmtree(tmp_path / "state" / "A")
    store = MemoryStore(load_snapshot_file(path))
    (b / "axon.yaml").write_text("repo: B\nservices: []\n")
    commit(b)
    index_remote(b, store, tmp_path)
    assert store.load().link_inputs["A"].source_generation == retained
    assert store.load().link_reports["A"].coverage["orders"]["outbound_external"] == 1


@pytest.mark.parametrize("when", ["before", "after"])
def test_cancellation_boundary_is_commit(tmp_path, when):
    import threading

    from axon.v1.indexer import IndexCancelled

    a, _ = remote_repos(tmp_path)
    event = threading.Event()

    class CancellingStore(MemoryStore):
        publish_progress = None

        def publish_repository(self, replacement, *, repo_id, validate=None):
            if when == "before":
                event.set()
            result = super().publish_repository(replacement, repo_id=repo_id, validate=validate)
            if when == "after":
                event.set()
            return result

    store = CancellingStore()
    if when == "before":
        with pytest.raises(IndexCancelled):
            index_remote(a, store, tmp_path, cancelled=event)
        assert not store.load().nodes
    else:
        _, _, result = index_remote(a, store, tmp_path, cancelled=event)
        assert result["publication_id"] == store.load().publication_id
    assert store.publish_progress is None


def test_target_publication_never_revalidates_caller_golden(tmp_path):
    a, b = remote_repos(tmp_path)
    store = MemoryStore()
    index_remote(a, store, tmp_path)
    # A golden that would fail (or fail parsing) must not be read during B indexing.
    (a / "axon.golden.yaml").write_text("invalid: [")
    index_remote(b, store, tmp_path)
    assert store.load().link_reports["A"].coverage["orders"]["outbound_resolved"] == 1


def test_link_refresh_does_not_invalidate_source_state(tmp_path):
    a, b = remote_repos(tmp_path)
    store = MemoryStore()
    index_remote(a, store, tmp_path)
    index_remote(b, store, tmp_path)
    _, _, result = index_remote(a, store, tmp_path)
    assert result["mode"] == "incremental"
    assert result["dirty_files"] == 0


def test_golden_repo_selector_and_external_url_evidence(tmp_path):
    from axon.v1.golden import verify_golden
    from axon.v1.repository_links import reconcile_repositories
    from tests.v1.test_multirepo_linking import caller, repository

    a, _ = remote_repos(tmp_path)
    snap = reconcile_repositories(caller(), repository("B", "ledger"), replace_repo_ids={"B"})
    for edge in snap.edges:
        if edge.kind.value == "CALLS_API":
            edge.evidence.file, edge.evidence.line = "orders/api.py", 5
    golden = a / "axon.golden.yaml"
    golden.write_text(
        "version: 1\ncases:\n  - api: {service: orders, method: GET, path: /orders}\n"
        "    expect: [{repo: C, service: ledger, path: /entries}]\n"
    )
    assert not verify_golden(snap, a, golden)["ok"]
    # The source-visible evidence is required only for matched edges/handlers.
    snap = caller()
    snap = reconcile_repositories(snap, repository("D", "other"), replace_repo_ids={"D"})
    for edge in snap.edges:
        if edge.kind.value == "CALLS_API":
            edge.evidence.file, edge.evidence.line = "orders/api.py", 5
    golden.write_text(
        "version: 1\ncases:\n  - api: {service: orders, method: GET, path: /orders}\n"
        "    expect: [{url: 'https://ledger.example/entries'}]\n"
    )
    assert verify_golden(snap, a, golden)["ok"]


def test_index_result_is_bound_to_its_returned_publication(tmp_path):
    a, b = remote_repos(tmp_path)

    class LaterWriter(MemoryStore):
        def publish_repository(self, replacement, *, repo_id, validate=None):
            committed = super().publish_repository(replacement, repo_id=repo_id, validate=validate)
            if repo_id == "A":
                target, _ = index_repo(b)
                super().publish_repository(target, repo_id="B")
            return committed

    store = LaterWriter()
    snap, _, result = index_remote(a, store, tmp_path)
    assert result["publication_id"] == snap.publication_id != store.load().publication_id
    assert result["relinked_repos"] == ["A"]
    assert snap.coverage_by_service("A")["orders"]["outbound_external"] == 1
    assert store.load().link_reports["A"].coverage["orders"]["outbound_resolved"] == 1


@pytest.mark.parametrize("error_kind", ["cancel", "progress", "state"])
def test_postcommit_failure_keeps_committed_success(tmp_path, monkeypatch, error_kind):
    from axon.v1.indexer import IndexCancelled
    from axon.v1.indexstate import StateStore

    a, _ = remote_repos(tmp_path)
    store = MemoryStore()

    def progress(phase, done, total):
        if store.load().publication_id and error_kind != "state":
            if error_kind == "cancel":
                raise IndexCancelled()
            raise RuntimeError("observer failed")

    if error_kind == "state":

        def fail(*args):
            raise RuntimeError("state backend failed")

        monkeypatch.setattr(StateStore, "save", fail)
    snap, _, result = index_remote(a, store, tmp_path, progress=progress)
    assert result["publication_id"] == snap.publication_id == store.load().publication_id
    assert result["warnings"]
    assert result["state_persisted"] is (error_kind != "state")


def test_target_api_scope_keeps_remote_incoming_callers(tmp_path):
    a, b = remote_repos(tmp_path)
    store = MemoryStore()
    index_remote(a, store, tmp_path)
    index_remote(b, store, tmp_path)
    _, _, result = index_remote(b, store, tmp_path, api="GET /entries")
    assert any(caller["repo_id"] == "A" for caller in result["callers"])
