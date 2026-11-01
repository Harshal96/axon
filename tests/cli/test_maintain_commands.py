import json

from typer.testing import CliRunner

from cli.app import app


def test_service_patch_dry_run_then_apply(tmp_path):
    path = tmp_path / "axon.yaml"
    original = "services: [{id: app, name: Old}]\n"
    path.write_text(original)
    args = ["service", "patch", "app", "--set", "name=New", "--repo", str(tmp_path), "--json"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["diffs"]
    assert path.read_text() == original
    result = CliRunner().invoke(app, [*args, "--apply"])
    assert result.exit_code == 0, result.output
    assert "New" in path.read_text()


def test_service_remove_json_choice(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "axon.yaml").write_text("services: [{id: app, roots: [src]}]\n")
    result = CliRunner().invoke(
        app, ["service", "remove", "app", "--repo", str(tmp_path), "--json"]
    )
    assert result.exit_code == 3, result.output
    assert json.loads(result.output)["candidates"]


def test_call_command_captures_evidence(tmp_path):
    (tmp_path / "axon.yaml").write_text("services: [{id: app}]\n")
    (tmp_path / "middleware.py").write_text("fraud.score(req)\n")
    result = CliRunner().invoke(
        app,
        [
            "call",
            "add",
            "score",
            "--service",
            "app",
            "--from-api",
            "POST /charge",
            "--to-service",
            "fraud",
            "--to-method",
            "POST",
            "--to-path",
            "/score",
            "--evidence",
            "middleware.py:1",
            "--repo",
            str(tmp_path),
            "--apply",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "fraud.score(req)" in (tmp_path / "axon.yaml").read_text()


def test_doctor_and_verify_json_errors_are_machine_readable(tmp_path):
    (tmp_path / "axon.yaml").write_text("services: [{id: app}]\n")
    result = CliRunner().invoke(app, ["doctor", "--repo", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"]
    result = CliRunner().invoke(app, ["verify", "--repo", str(tmp_path), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]
