"""CLI adapter for reviewed graph evidence sampling."""

import json
from pathlib import Path

import typer
import yaml  # type: ignore[import-untyped]
from rich.console import Console

from axon.v2.indexer import index_repo
from axon.v2.servicemap import load_service_map
from axon.v2.store import load_snapshot_file
from axon.v2.verify_sample import apply_verdicts, export_golden, sample_edges


def register(app: typer.Typer) -> None:
    app.command("verify-sample")(verify_sample_cmd)


def verify_sample_cmd(
    repo: str = typer.Option(..., "--repo"),
    services: int = typer.Option(5, "--services", min=1),
    apis: int = typer.Option(5, "--apis", min=1),
    seed: int = typer.Option(0, "--seed"),
    snapshot: str | None = typer.Option(None, "--snapshot"),
    out: str | None = typer.Option(None, "--out", help="Save review JSON"),
    review: str | None = typer.Option(None, "--review", help="JSON with reviewer verdicts"),
    golden: str | None = typer.Option(None, "--export-golden", help="Export confirmed rows"),
) -> None:
    """Sample cross-service calls and export human-confirmed regression cases."""
    console = Console()
    try:
        root = Path(repo).resolve()
        snap = load_snapshot_file(snapshot) if snapshot else index_repo(root)[0]
        report = sample_edges(
            snap, root, services=services, apis=apis, seed=seed, repo_id=load_service_map(root).repo
        )
        if review:
            apply_verdicts(report, json.loads(Path(review).read_text()))
        text = json.dumps(report, indent=2)
        if out:
            Path(out).write_text(text + "\n")
        if golden:
            data = export_golden(report)
            if not data["cases"]:
                raise ValueError("no confirmed rows to export; mark verdicts ok in review JSON")
            Path(golden).write_text(yaml.safe_dump(data, sort_keys=False))
        console.print(text, markup=False)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        console.print(str(exc), markup=False)
        raise typer.Exit(1) from exc
