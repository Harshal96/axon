import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli.app import app


@pytest.mark.parametrize(
    "agent,directory",
    [
        ("codex", ".agents/skills"),
        ("cursor", ".agents/skills"),
        ("claude", ".claude/skills"),
        ("devin", ".devin/skills"),
    ],
)
def test_install_and_protect_local_changes(tmp_path, agent, directory):
    args = ["skills", "install", "--project", "--repo", str(tmp_path), "--agent", agent, "--json"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["files"]
    target = tmp_path / directory / "axon-onboard" / "SKILL.md"
    assert "axon-version:" in target.read_text()
    target.write_text(target.read_text() + "\nLocal customizations\n")
    before = target.read_bytes()
    result = CliRunner().invoke(app, ["skills", "update", *args[2:]])
    assert result.exit_code == 4, result.output
    assert target.read_bytes() == before
    result = CliRunner().invoke(app, ["skills", "update", *args[2:], "--force"])
    assert result.exit_code == 0, result.output
    assert "Local customizations" not in target.read_text()


def test_uninstall_refuses_unmanaged_directory(tmp_path):
    target = tmp_path / ".agents/skills/axon-onboard"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("user-owned")
    result = CliRunner().invoke(
        app, ["skills", "uninstall", "--project", "--repo", str(tmp_path), "--json"]
    )
    assert result.exit_code == 4
    assert (target / "SKILL.md").read_text() == "user-owned"


def test_global_install_uses_agent_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = CliRunner().invoke(
        app, ["skills", "install", "--global", "--agent", "devin", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".config/devin/skills/axon-onboard/SKILL.md").exists()


def test_installed_skill_version_doctor(tmp_path, monkeypatch):
    from cli.commands import skills
    from cli.commands.skills import installed_skill_issues

    monkeypatch.setattr(skills, "_version", lambda: "0.10.0")
    monkeypatch.setattr(skills.Path, "home", lambda: tmp_path / "home")
    folder = tmp_path / ".agents/skills/axon-maintain"
    folder.mkdir(parents=True)
    path = folder / "SKILL.md"
    path.write_text("---\naxon-version: 0.9.0\n---\n")
    issues = installed_skill_issues(tmp_path)
    assert len(issues) == 1
    assert issues[0]["code"] == "skill-outdated"
    assert str(path) in issues[0]["message"]
    path.write_text("---\naxon-version: 0.10.0\n---\n")
    assert installed_skill_issues(tmp_path) == []
    path.write_text("---\naxon-version: 0.11.0\n---\n")
    assert installed_skill_issues(tmp_path) == []


def test_skill_doctor_prerelease_and_global_locations(tmp_path, monkeypatch):
    from cli.commands import skills

    monkeypatch.setattr(skills, "_version", lambda: "1.0.0")
    monkeypatch.setattr(skills.Path, "home", lambda: tmp_path / "home")
    directory = tmp_path / "home/.claude/skills/axon-maintain"
    directory.mkdir(parents=True)
    target = directory / "SKILL.md"
    target.write_text("---\naxon-version: 1.0.0rc1\n---\n")
    assert len(skills.installed_skill_issues(tmp_path)) == 1
    target.write_text("---\naxon-version: 1.0.0+local\n---\n")
    assert skills.installed_skill_issues(tmp_path) == []
    target.write_text("---\nname: axon-maintain\n---\n")
    assert skills.installed_skill_issues(tmp_path) == []
