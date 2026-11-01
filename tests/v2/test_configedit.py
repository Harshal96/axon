from pathlib import Path

import pytest

from axon.v2.configedit import ConfigEditor, EditError
from axon.v2.servicemap import load_service_map, load_source_map


def test_comment_preserving_dry_run_and_apply(tmp_path):
    path = tmp_path / "axon.yaml"
    before = '# curated\nservices:\n  - id: app # keep this\n    name: "Old"\n    roots: [src]\n'
    path.write_text(before)
    editor = ConfigEditor(tmp_path)
    plan = editor.patch("service:app", {"name": "New"})
    assert '-    name: "Old"' in plan.diffs[str(path)]
    assert path.read_text() == before
    editor.apply(plan)
    assert path.read_text() == before.replace('"Old"', '"New"')


def test_generated_removal_writes_tombstone(tmp_path):
    generated = tmp_path / "base.yaml"
    generated.write_text("# axon:generated\nservices: [{id: app, roots: [src]}]\n")
    (tmp_path / "axon.yaml").write_text("extends: base.yaml\noverrides: overrides.yaml\n")
    (tmp_path / "overrides.yaml").write_text("# team corrections\nservices: []\n")
    editor = ConfigEditor(tmp_path)
    plan = editor.remove("service:app")
    editor.apply(plan)
    assert "+delete: true" in (tmp_path / "overrides.yaml").read_text()
    assert generated.read_text().startswith("# axon:generated\nservices: [{id: app")
    assert load_service_map(tmp_path).services == []


def test_team_target_created_only_on_apply(tmp_path):
    (tmp_path / "services/app").mkdir(parents=True)
    (tmp_path / "axon.yaml").write_text("""services: [{id: app, roots: [services/app]}]
overrides: ["services/**/axon.service.yaml"]
""")
    editor = ConfigEditor(tmp_path)
    plan = editor.patch("service:app", {"name": "New"})
    target = tmp_path / "services/app/axon.service.yaml"
    assert not target.exists()
    editor.apply(plan)
    assert load_service_map(tmp_path).services[0].name == "New"
    assert target.exists()


def test_invalid_and_concurrent_plans_never_write(tmp_path):
    path = tmp_path / "axon.yaml"
    path.write_text("services: [{id: app}]\n")
    editor = ConfigEditor(tmp_path)
    with pytest.raises(EditError):
        editor.patch("service:app", {"id": "bad id"})
    plan = editor.patch("service:app", {"name": "New"})
    path.write_text("services: [{id: app, name: Concurrent}]\n")
    with pytest.raises(EditError, match="changed"):
        editor.apply(plan)
    assert "Concurrent" in path.read_text()


def test_source_map_tracks_patch_and_delete(tmp_path):
    (tmp_path / "base.yaml").write_text("services: [{id: a}, {id: b}]\n")
    (tmp_path / "axon.yaml").write_text(
        "extends: base.yaml\nservices: [{id: a, name: New}, {id: b, +delete: true}]\n"
    )
    sources = load_source_map(tmp_path)
    assert [s.op for s in sources.entities["service:a"]] == ["define", "patch"]
    assert sources.entities["service:b"][-1].op == "delete"
    assert Path(sources.entities["service:a"][0].file).name == "base.yaml"


def test_doctor_rejects_new_errors_without_writes(tmp_path):
    path = tmp_path / "axon.yaml"
    path.write_text("services: [{id: app, roots: [existing]}]\n")
    (tmp_path / "existing").mkdir()
    original = path.read_text()
    editor = ConfigEditor(tmp_path)
    with pytest.raises(EditError, match="missing-root"):
        editor.patch("service:app", {"roots": ["missing"]})
    assert path.read_text() == original


def test_existing_doctor_errors_and_warnings_do_not_block(tmp_path):
    path = tmp_path / "axon.yaml"
    path.write_text("services: [{id: app, roots: [missing]}]\n")
    editor = ConfigEditor(tmp_path)
    plan = editor.patch("service:app", {"name": "Renamed"})
    editor.apply(plan)
    assert load_service_map(tmp_path).services[0].name == "Renamed"


def test_apply_revalidates_tampered_plan_and_generated_sources(tmp_path):
    path = tmp_path / "axon.yaml"
    path.write_text("extends: base.yaml\nservices: [{id: app, roots: []}]\n")
    generated = tmp_path / "base.yaml"
    generated.write_text("# axon:generated\nservices: [{id: inherited}]\n")
    editor = ConfigEditor(tmp_path)
    plan = editor.patch("service:app", {"name": "Renamed"})
    original = path.read_text()
    plan.after[str(path)] = "extends: base.yaml\nservices: [{id: app, roots: [absent]}]\n"
    with pytest.raises(EditError, match="missing-root"):
        editor.apply(plan)
    assert path.read_text() == original
    plan = editor.patch("service:app", {"name": "Renamed"})
    plan.after[str(generated)] = "services: [{id: changed}]\n"
    with pytest.raises(EditError, match="generated"):
        editor.apply(plan)
    assert generated.read_text().startswith("# axon:generated")


def test_new_glob_dependency_blocks_apply(tmp_path):
    root = tmp_path / "svc"
    root.mkdir()
    path = tmp_path / "axon.yaml"
    path.write_text('services: [{id: app, roots: [svc]}]\noverrides: ["svc/*.yaml"]\n')
    editor = ConfigEditor(tmp_path)
    plan = editor.patch("service:app", {"name": "Renamed"})
    (root / "new.yaml").write_text("services: [{id: app, name: Concurrent}]\n")
    with pytest.raises(EditError, match="changed"):
        editor.apply(plan)
    assert "Renamed" not in path.read_text()


def test_apply_rechecks_dependencies_after_staging(tmp_path, monkeypatch):
    import os

    path = tmp_path / "axon.yaml"
    path.write_text("services: [{id: app}]\n")
    editor = ConfigEditor(tmp_path)
    plan = editor.patch("service:app", {"name": "Planned"})
    fsync = os.fsync
    changed = False

    def concurrent_change(fd):
        nonlocal changed
        fsync(fd)
        if not changed:
            changed = True
            path.write_text("services: [{id: app, name: Concurrent}]\n")

    monkeypatch.setattr(os, "fsync", concurrent_change)
    with pytest.raises(EditError, match="changed"):
        editor.apply(plan)
    assert "Concurrent" in path.read_text()
    assert not list(tmp_path.glob(".axon-*"))


def test_apply_rechecks_doctor_when_source_files_disappear(tmp_path):
    path = tmp_path / "axon.yaml"
    path.write_text("services: [{id: app}]\n")
    (tmp_path / "new").mkdir()
    editor = ConfigEditor(tmp_path)
    plan = editor.patch("service:app", {"roots": ["new"]})
    (tmp_path / "new").rmdir()
    with pytest.raises(EditError, match="missing-root"):
        editor.apply(plan)
    assert path.read_text() == "services: [{id: app}]\n"


def test_doctor_warnings_are_allowed(tmp_path):
    path = tmp_path / "axon.yaml"
    path.write_text("services: [{id: app}]\n")
    editor = ConfigEditor(tmp_path)
    plan = editor.patch("service:app", {"clients": [{"id": "sdk", "packages": ["unused_sdk"]}]})
    editor.apply(plan)
    assert load_service_map(tmp_path).services[0].clients[0].id == "sdk"


def test_patch_changes_effective_value_after_later_overlay(tmp_path):
    (tmp_path / "axon.yaml").write_text(
        "services: [{id: app, name: Base}]\noverrides: overlay.yaml\n"
    )
    (tmp_path / "overlay.yaml").write_text("services: [{id: app, name: Override}]\n")
    editor = ConfigEditor(tmp_path)
    plan = editor.patch("service:app", {"name": "Wanted"})
    editor.apply(plan)
    assert load_service_map(tmp_path).services[0].name == "Wanted"


def test_generated_later_overlay_cannot_mask_patch(tmp_path):
    (tmp_path / "axon.yaml").write_text(
        "services: [{id: app, name: Base}]\noverrides: [generated.yaml, editable.yaml]\n"
    )
    (tmp_path / "generated.yaml").write_text(
        "# axon:generated\nservices: [{id: app, name: Generated}]\n"
    )
    (tmp_path / "editable.yaml").write_text("services: []\n")
    editor = ConfigEditor(tmp_path)
    plan = editor.patch("service:app", {"name": "Wanted"})
    editor.apply(plan)
    assert load_service_map(tmp_path).services[0].name == "Wanted"


def test_refs_include_package_links_and_endpoint_selectors(tmp_path):
    from axon.v2.configedit import config_refs

    (tmp_path / "axon.yaml").write_text("""repo: r
services:
- id: app
  clients: [{id: sdk, packages: [ledger_sdk]}]
  handlers: [{id: fix, api: "GET /a", handler: app.a}]
links: [{call_name: ledger_sdk.charge, to_service: app, to_path: /a}]
""")
    (tmp_path / "axon.golden.yaml").write_text("""version: 1
cases:
- id: case
  api: {service: app, method: GET, path: /a}
  expect: [{service: app, method: GET, path: /a}]
""")
    refs = config_refs(tmp_path, "client:sdk")
    assert any("links" in r.yaml_path for r in refs)
    refs = config_refs(tmp_path, "endpoint:endpoint::r::app::GET:/a")
    assert any("handlers" in r.yaml_path for r in refs)
    assert any(r.file.endswith("axon.golden.yaml") for r in refs)
