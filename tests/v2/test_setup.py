import pytest

from axon.v2.servicemap import load_service_map


def test_overlays_merge_service_and_client_ids_and_append_lists(tmp_path):
    (tmp_path / "base.yaml").write_text(
        """repo: sample
ignore: [vendor]
services:
  - id: app
    roots: [app]
    clients: [{id: sdk, packages: [old], target_service: remote}]
  - id: remote
    roots: [remote]
"""
    )
    (tmp_path / "axon.yaml").write_text("extends: base.yaml\noverrides: axon.overrides.yaml\n")
    (tmp_path / "axon.overrides.yaml").write_text(
        """ignore: {+append: [noise]}
services:
  - id: app
    clients: [{id: sdk, packages: [new]}]
"""
    )
    sm = load_service_map(tmp_path)
    assert sm.ignore == ["vendor", "noise"]
    assert len(sm.services) == 2
    assert sm.services[0].roots == ["app"]
    assert sm.services[0].clients[0].packages == ["new"]
    assert sm.services[0].clients[0].target_service == "remote"


def test_overlay_cycles_fail_with_useful_error(tmp_path):
    (tmp_path / "axon.yaml").write_text("extends: base.yaml\n")
    (tmp_path / "base.yaml").write_text("extends: axon.yaml\n")
    with pytest.raises(ValueError, match="cycle"):
        load_service_map(tmp_path)


def test_overlay_bytes_change_fingerprint(tmp_path):
    from axon.v2.servicemap import service_map_fingerprint

    (tmp_path / "axon.yaml").write_text("overrides: local.yaml\nservices: []\n")
    overlay = tmp_path / "local.yaml"
    overlay.write_text("ignore: [one]\n")
    before = service_map_fingerprint(tmp_path)
    overlay.write_text("ignore: [two]\n")
    assert service_map_fingerprint(tmp_path) != before


def test_index_fingerprint_detects_all_config_edits(tmp_path, monkeypatch):
    monkeypatch.setattr("axon.v2.revision.checkout_dirty", lambda root: False)
    monkeypatch.setattr("axon.v2.indexer.checkout_dirty", lambda root: False)
    from axon.v2.api import _already_indexed
    from axon.v2.indexer import index_repo
    from axon.v2.store import MemoryStore

    config = tmp_path / "axon.yaml"
    config.write_text("repo: sample\nservices: []\n")
    store = MemoryStore()
    snap, _ = index_repo(tmp_path, commit="abc")
    store.save(snap)
    assert _already_indexed(store, tmp_path, "abc")
    config.write_text("repo: sample\nservices: []\nignore: [noise]\n")
    assert _already_indexed(store, tmp_path, "abc") is None


def test_dirty_checkout_is_labelled_and_never_skipped(tmp_path):
    import subprocess

    from axon.v2.api import _already_indexed
    from axon.v2.indexer import index_repo
    from axon.v2.models import NodeKind
    from axon.v2.store import MemoryStore

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "axon.yaml").write_text("repo: sample\nservices: []\n")
    snap, _ = index_repo(tmp_path, commit="abc")
    assert snap.by_kind(NodeKind.REPO)[0].props["dirty"] == "true"
    store = MemoryStore()
    store.save(snap)
    assert _already_indexed(store, tmp_path, "abc") is None


def test_doctor_reports_missing_roots_specs_targets_and_dead_clients(tmp_path):
    from axon.v2.doctor import doctor

    (tmp_path / "axon.yaml").write_text(
        """services:
  - id: app
    roots: [missing]
    specs: [missing.yaml]
    clients: [{id: sdk, packages: [unused_sdk], target_service: absent}]
"""
    )
    report = doctor(tmp_path)
    assert not report["ok"]
    text = str(report)
    assert "missing-root" in text
    assert "missing-spec" in text
    assert "unknown-target" in text
    assert "unused-client" in text


def test_golden_expected_forbidden_and_evidence_snippets(tmp_path):
    import yaml

    from axon.v2.golden import verify_golden
    from axon.v2.indexer import index_repo

    (tmp_path / "axon.yaml").write_text("repo: sample\nservices: [{id: app, roots: [app]}]\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text(
        """from fastapi import FastAPI
import requests
app=FastAPI()
@app.get('/local')
def run(): requests.get('/remote')
@app.get('/remote')
def remote(): pass
"""
    )
    snap, _ = index_repo(tmp_path)
    golden = tmp_path / "axon.golden.yaml"
    case = {
        "id": "local-call",
        "api": {"service": "app", "method": "GET", "path": "/local"},
        "expect": [
            {"service": "app", "path": "/remote", "evidence": {"file": "app/main.py", "line": 5}}
        ],
    }
    golden.write_text(yaml.safe_dump({"version": 1, "cases": [case]}))
    result = verify_golden(snap, tmp_path, golden)
    assert result["ok"]
    assert "requests.get" in str(result)
    case["forbid"] = [{"service": "app", "path": "/remote"}]
    golden.write_text(yaml.safe_dump({"version": 1, "cases": [case]}))
    assert not verify_golden(snap, tmp_path, golden)["ok"]


def test_resolution_report_includes_empty_services_and_rule_buckets(tmp_path):
    from axon.v2.indexer import index_repo

    (tmp_path / "axon.yaml").write_text(
        "repo: sample\nservices: [{id: app, roots: [app]}, {id: empty, roots: [empty]}]\n"
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("import requests\ndef run(url): requests.get(url)\n")
    snap, _ = index_repo(tmp_path)
    report = snap.resolution_report("sample")
    assert report["app"]["unresolved_rules"] == {"unresolved:requests": 1}
    assert report["empty"]["endpoints"] == 0
    assert report["empty"]["handler_percent"] == 0


def test_git_collection_keeps_untracked_and_skips_deleted(tmp_path):
    import subprocess

    from axon.v2.indexer import collect_files
    from axon.v2.servicemap import ServiceMap

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "main.py").write_text("pass")
    (tmp_path / "gone.py").write_text("pass")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    (tmp_path / "gone.py").unlink()
    (tmp_path / "new.py").write_text("pass")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv/noise.py").write_text("pass")
    assert collect_files(tmp_path, ServiceMap()) == ["main.py", "new.py"]


def test_doctor_checks_discovered_specs_and_shared_overlap(tmp_path):
    from axon.v2.doctor import doctor

    (tmp_path / "app").mkdir()
    (tmp_path / "app/openapi.yaml").write_text("not an openapi document")
    (tmp_path / "axon.yaml").write_text("services: [{id: app, roots: [app]}]\nshared: [app]\n")
    codes = {i["code"] for i in doctor(tmp_path)["issues"]}
    assert {"invalid-spec", "root-inside-shared"} <= codes


def test_doctor_recognizes_from_import_package_member(tmp_path):
    from axon.v2.doctor import doctor

    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("from generated import sdk\n")
    (tmp_path / "axon.yaml").write_text(
        "services: [{id: app, roots: [app], clients: [{id: sdk, packages: [generated.sdk]}]}]\n"
    )
    assert not any(i["code"] == "unused-client" for i in doctor(tmp_path)["issues"])


def test_unknown_checkout_state_never_skips(tmp_path):
    from axon.v2.api import _already_indexed
    from axon.v2.indexer import index_repo
    from axon.v2.store import MemoryStore

    (tmp_path / "axon.yaml").write_text("repo: sample\nservices: []\n")
    snap, _ = index_repo(tmp_path, commit="abc")
    store = MemoryStore()
    store.save(snap)
    assert _already_indexed(store, tmp_path, "abc") is None
