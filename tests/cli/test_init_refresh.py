from typer.testing import CliRunner

from cli.app import app


def test_init_refresh_proposes_diff_without_overwriting(tmp_path):
    (tmp_path / "axon.yaml").write_text("repo: sample\nservices: []\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("app = FastAPI()\n")
    before = (tmp_path / "axon.yaml").read_bytes()
    result = CliRunner().invoke(app, ["init", "--repo", str(tmp_path), "--refresh"])
    assert result.exit_code == 0, result.output
    assert "proposed axon.yaml diff" in result.output
    assert "Proposed addition" in result.output
    assert (tmp_path / "axon.yaml").read_bytes() == before
    result = CliRunner().invoke(app, ["init", "--repo", str(tmp_path), "--refresh", "--write"])
    assert result.exit_code == 1
    assert "refuses to overwrite" in result.output
    assert (tmp_path / "axon.yaml").read_bytes() == before
