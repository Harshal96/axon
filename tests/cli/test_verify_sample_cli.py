import json

import yaml
from typer.testing import CliRunner

from cli.app import app
from tests.v2.test_verify_sample import fixture_graph


def test_seeded_sample_cli_and_reviewed_golden_roundtrip(tmp_path):
    from axon.v2.golden import verify_golden
    from axon.v2.store import save_snapshot_file

    (tmp_path / "axon.yaml").write_text("repo: sample\nservices: []\n")
    snap = fixture_graph(tmp_path)
    for node in snap.nodes.values():
        node.repo_id = "sample"
    snapshot = tmp_path / "graph.json"
    review = tmp_path / "review.json"
    golden = tmp_path / "axon.golden.yaml"
    save_snapshot_file(snap, str(snapshot))
    args = [
        "verify-sample",
        "--repo",
        str(tmp_path),
        "--snapshot",
        str(snapshot),
        "--seed",
        "7",
        "--out",
        str(review),
    ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    report = json.loads(review.read_text())
    report["rows"][0]["verdict"] = "ok"
    review.write_text(json.dumps(report))
    result = CliRunner().invoke(
        app, args[:-2] + ["--review", str(review), "--export-golden", str(golden)]
    )
    assert result.exit_code == 0, result.output
    assert len(yaml.safe_load(golden.read_text())["cases"]) == 1
    assert verify_golden(snap, tmp_path, golden)["ok"]
