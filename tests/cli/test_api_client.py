from __future__ import annotations

from pathlib import Path

from cli.api_client import resolve_config, save_config

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "v2" / "monorepo"


def test_config_resolution_order(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AXON_API_URL", raising=False)
    default = resolve_config()
    assert default.api_url == "http://localhost:8080"
    assert default.configured is False

    save_config("http://file-host:9000", "file-token")
    from_file = resolve_config()
    assert from_file.api_url == "http://file-host:9000"
    assert from_file.token == "file-token"
    assert from_file.configured is True

    monkeypatch.setenv("AXON_API_URL", "http://env-host:9001")
    assert resolve_config().api_url == "http://env-host:9001"

    explicit = resolve_config("http://flag-host:9002/")
    assert explicit.api_url == "http://flag-host:9002"
    assert explicit.configured is True


def test_configure_command_writes_shared_config(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from cli import app as cli_app_module

    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli_app_module.app, ["configure", "--api-url", "http://box:8080"])
    assert result.exit_code == 0, result.output
    assert "http://box:8080" in result.output
    assert (tmp_path / ".axon" / "config.json").exists()
    assert resolve_config().api_url == "http://box:8080"


def test_watch_job_polls_to_done():
    from cli.app import _watch_job

    states = [
        {"status": "running", "phase": "parse", "done": 5, "total": 10},
        {"status": "running", "phase": "calls", "done": 0, "total": 1},
        {"status": "done", "phase": "done", "done": 1, "total": 1, "result": {"repo": "r"}},
    ]

    class FakeClient:
        def poll_job(self, job_id, on_update):
            for i, s in enumerate(states):
                on_update(s, float(i))
            return states[-1]

    job = _watch_job(FakeClient(), "job1", "indexing")  # type: ignore[arg-type]
    assert job["status"] == "done"


def test_index_without_server_explains_configuration(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from cli import app as cli_app_module

    monkeypatch.delenv("AXON_API_URL", raising=False)
    result = CliRunner().invoke(
        cli_app_module.app,
        ["index", "--repo", str(FIXTURE), "--api-url", "http://127.0.0.1:9"],
    )
    assert result.exit_code == 1
    assert "cannot reach Axon API" in result.output
    assert "configure --api-url" in result.output
