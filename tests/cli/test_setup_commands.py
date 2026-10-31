from typer.testing import CliRunner

from cli.app import app


def test_doctor_command_missing_root_exits_one(tmp_path):
    (tmp_path / "axon.yaml").write_text("services: [{id: app, roots: [missing]}]\n")
    result = CliRunner().invoke(app, ["doctor", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "missing-root" in result.output


def test_verify_command_rejects_empty_contract(tmp_path):
    (tmp_path / "axon.yaml").write_text("services: []\n")
    (tmp_path / "axon.golden.yaml").write_text("version: 1\ncases: []\n")
    result = CliRunner().invoke(
        app, ["verify", "--repo", str(tmp_path), "--golden", "axon.golden.yaml"]
    )
    assert result.exit_code == 1
    assert "nonempty cases" in result.output
