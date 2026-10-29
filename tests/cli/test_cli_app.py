from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app as cli_app_module

pytestmark = pytest.mark.tier1

runner = CliRunner()
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "v2" / "monorepo"


def test_index_command_reports_parse_warnings_once(tmp_path) -> None:
    (tmp_path / "axon.yaml").write_text(
        "version: 1\nrepo: r\nservices:\n  - id: svc\n    roots: [svc]\n    language: python\n",
        encoding="utf-8",
    )
    svc = tmp_path / "svc"
    svc.mkdir()
    (svc / "a.py").write_text("PAT = '\\d'\n\ndef f():\n    return 1\n", encoding="utf-8")
    result = runner.invoke(
        cli_app_module.app, ["index", "--repo", str(tmp_path), "--no-cache", "--local"]
    )
    assert result.exit_code == 0, result.output
    assert result.output.count("parse warnings:") == 1
    assert "<unknown>:" not in result.output


def test_index_command_lists_services() -> None:
    result = runner.invoke(cli_app_module.app, ["index", "--repo", str(FIXTURE), "--local"])
    assert result.exit_code == 0, result.output
    assert "payment" in result.output


def test_init_ai_without_configured_model_fails_cleanly(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "none")
    from axon.config import get_settings

    get_settings.cache_clear()
    try:
        result = runner.invoke(
            cli_app_module.app, ["init", "--repo", str(tmp_path), "--local", "--ai"]
        )
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 1
    assert "LLM_PROVIDER" in result.output


def test_apis_command() -> None:
    result = runner.invoke(cli_app_module.app, ["apis", "--repo", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    assert "/v1/charges" in result.output


def test_ask_command() -> None:
    result = runner.invoke(
        cli_app_module.app,
        ["ask", "What services does POST /checkout call?", "--repo", str(FIXTURE)],
    )
    assert result.exit_code == 0, result.output
    assert "payment" in result.output
