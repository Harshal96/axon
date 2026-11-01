import pytest

from axon.v1.configedit import ConfigEditor, EditError
from axon.v1.maintenance import move_client, remove_client, remove_service, rename_entity
from axon.v1.servicemap import load_service_map


@pytest.fixture
def repo(tmp_path):
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    (tmp_path / "axon.yaml").write_text("""services:
- id: a
  roots: [a]
  clients: [{id: sdk, packages: [ledger_sdk], target_service: b}]
- id: b
  roots: [b]
- id: c
  roots: [c]
  clients: [{id: other, target_service: a}]
  links: [{id: to-a, call_name: charge, to_service: a, to_path: /a}]
""")
    return tmp_path


def test_service_remove_requires_code_choice_and_cascades(repo):
    with pytest.raises(EditError) as exc:
        remove_service(ConfigEditor(repo), "a")
    assert exc.value.code == 3
    plan = remove_service(ConfigEditor(repo), "a", ignore_code=True)
    ConfigEditor(repo).apply(plan)
    sm = load_service_map(repo)
    assert [s.id for s in sm.services] == ["b", "c"]
    assert sm.ignore == ["a"]
    assert sm.services[-1].clients[0].target_service == ""
    assert sm.services[-1].links == []


def test_client_guard_reports_code_and_force_removes(repo):
    (repo / "c/use.py").write_text("import ledger_sdk\n")
    with pytest.raises(EditError) as exc:
        remove_client(ConfigEditor(repo), "sdk")
    assert exc.value.code == 4
    assert exc.value.candidates[0]["file"] == "c/use.py"
    plan = remove_client(ConfigEditor(repo), "sdk", force=True)
    ConfigEditor(repo).apply(plan)
    assert load_service_map(repo).services[0].clients == []


def test_service_rename_updates_all_references(repo):
    plan = rename_entity(ConfigEditor(repo), "service:a", "payments")
    ConfigEditor(repo).apply(plan)
    sm = load_service_map(repo)
    assert sm.services[0].id == "payments"
    assert sm.services[-1].clients[0].target_service == "payments"
    assert sm.services[-1].links[0].to_service == "payments"


def test_move_client_changes_owner_not_identity(repo):
    editor = ConfigEditor(repo)
    plan = move_client(editor, "sdk", "c")
    editor.apply(plan)
    sm = load_service_map(repo)
    assert sm.services[0].clients == []
    assert sm.services[-1].clients[-1].id == "sdk"


def test_generated_client_rename_uses_tombstone(repo):
    original = (repo / "axon.yaml").read_text()
    (repo / "base.yaml").write_text("# axon:generated\n" + original)
    (repo / "axon.yaml").write_text("extends: base.yaml\noverrides: overrides.yaml\n")
    (repo / "overrides.yaml").write_text("services: []\n")
    editor = ConfigEditor(repo)
    plan = rename_entity(editor, "client:sdk", "new-sdk")
    editor.apply(plan)
    assert load_service_map(repo).services[0].clients[0].id == "new-sdk"
    assert (repo / "base.yaml").read_text() == "# axon:generated\n" + original


def test_service_remove_keeps_cross_repo_targets(repo):
    path = repo / "axon.yaml"
    path.write_text(
        path.read_text()
        + """links:
- {id: remote-a, call_name: remote, to_service: a, to_repo: other, to_path: /a}
"""
    )
    editor = ConfigEditor(repo)
    editor.apply(remove_service(editor, "a", leave_unowned=True))
    assert load_service_map(repo).links[0].id == "remote-a"


def test_generated_service_rename_updates_self_references(repo):
    original = """services:
- id: a
  roots: [a]
  links: [{id: self, call_name: selfcall, to_service: a, to_path: /a}]
  clients: [{id: sdk, target_service: a}]
"""
    (repo / "base.yaml").write_text("# axon:generated\n" + original)
    (repo / "axon.yaml").write_text("extends: base.yaml\noverrides: overrides.yaml\n")
    (repo / "overrides.yaml").write_text("services: []\n")
    editor = ConfigEditor(repo)
    editor.apply(rename_entity(editor, "service:a", "new"))
    sm = load_service_map(repo)
    assert [s.id for s in sm.services] == ["new"]
    assert sm.services[0].links[0].to_service == "new"
    assert sm.services[0].clients[0].target_service == "new"


def test_legacy_link_cascade_edits_winning_overlay(repo):
    (repo / "axon.yaml").write_text((repo / "axon.yaml").read_text() + "overrides: overlay.yaml\n")
    (repo / "overlay.yaml").write_text(
        "links: [{call_name: ledger_sdk.charge, to_service: b, to_path: /b}]\n"
    )
    editor = ConfigEditor(repo)
    editor.apply(remove_client(editor, "sdk", cascade_links=True))
    assert load_service_map(repo).links == []


def test_ignore_code_for_generated_root_writes_editable_override(repo):
    path = repo / "axon.yaml"
    original = "# axon:generated\n" + path.read_text() + "overrides: overrides.yaml\n"
    path.write_text(original)
    (repo / "overrides.yaml").write_text("services: []\n")
    editor = ConfigEditor(repo)
    editor.apply(remove_service(editor, "a", ignore_code=True))
    assert "a" in load_service_map(repo).ignore
    assert path.read_text() == original
