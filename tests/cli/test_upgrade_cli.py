from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from cli.app import app

COMPAT = Path(__file__).resolve().parents[1] / "fixtures" / "compat"
runner = CliRunner()


def test_upgrade_check_snapshot_json():
    result = runner.invoke(
        app, ["upgrade-check", "--snapshot", str(COMPAT / "snapshot-v1.json"), "--json"]
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["verdict"] == "compatible"
    assert report["checks"]["snapshot"]["status"] == "compatible"


def test_upgrade_check_too_new_snapshot_blocked(tmp_path):
    data = json.loads((COMPAT / "snapshot-v1.json").read_text())
    data["schema_version"] = 99
    target = tmp_path / "new.json"
    target.write_text(json.dumps(data))
    result = runner.invoke(app, ["upgrade-check", "--snapshot", str(target), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["verdict"] == "blocked"


def test_graph_compare_cli(tmp_path):
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    before.write_text(json.dumps({"nodes": {}, "edges": []}))
    after.write_text(json.dumps({"nodes": {}, "edges": []}))
    result = runner.invoke(
        app, ["graph-compare", "--before", str(before), "--after", str(after), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["nodes_before"] == 0


def test_upgrade_check_help_lists_command():
    result = runner.invoke(app, ["--help"])
    assert "upgrade-check" in result.output
    assert "graph-rollback" in result.output
    assert "snapshot-import" in result.output
