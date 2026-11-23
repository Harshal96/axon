from __future__ import annotations

import anyio
import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.prompt import Prompt
from rich.table import Table

from axon.v1 import ask as ask_mod
from axon.v1.graph import GraphSnapshot
from axon.v1.impact import ImpactResult, compute_impact, git_changed_files, render_markdown
from axon.v1.indexer import index_repo
from axon.v1.models import NodeKind
from axon.v1.onboard import refresh_repo, scan_repo
from axon.v1.store import Neo4jStore, load_snapshot_file, save_snapshot_file
from cli.api_client import ApiClient, ApiError, resolve_config, save_config
from cli.commands.diagnose import register as register_diagnose
from cli.commands.maintain import register as register_maintain
from cli.commands.skills import app as skills_app
from cli.verify_sample import register as register_verify_sample

app = typer.Typer(help="Axon v1: API-first code graph")
app.add_typer(skills_app, name="skills")
register_maintain(app)
register_diagnose(app)
register_verify_sample(app)
console = Console()

jobs_app = typer.Typer(help="Watch or cancel background jobs on the API server")
app.add_typer(jobs_app, name="jobs")
repo_app = typer.Typer(help="Manage repository graph ownership on the API server")
app.add_typer(repo_app, name="repo")


@repo_app.command("remove")
def repo_remove(
    repo_id: str = typer.Option(..., "--repo-id"),
    api_url: str | None = typer.Option(None, "--api-url"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Remove a repository from the graph and reconcile its retained callers."""
    client = _client_or_exit(api_url)
    try:
        result = client.remove_repository(repo_id)
    except ApiError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2) from exc
    finally:
        client.close()
    if json_output:
        typer.echo(__import__("json").dumps(result))
    else:
        console.print(f"Removed {repo_id}; publication {result['publication_id']}")


def _progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("elapsed"),
        TimeElapsedColumn(),
        TextColumn("eta"),
        TimeRemainingColumn(),
        console=console,
    )


def _watch_job(client: ApiClient, job_id: str, label: str) -> dict:
    """Live-watch a server job. Ctrl-C detaches; the job keeps running."""
    try:
        with _progress() as progress:
            task = progress.add_task(label, total=None)

            def on_update(job: dict, _elapsed: float) -> None:
                phase = str(job.get("phase") or job.get("status") or "")
                done = int(job.get("done") or 0)
                total = int(job.get("total") or 0)
                progress.update(
                    task,
                    description=f"{label} [{phase}]" if phase else label,
                    total=total if total > 1 else None,
                    completed=done,
                )

            job = client.poll_job(job_id, on_update)
    except KeyboardInterrupt:
        console.print(
            f"\n[yellow]Detached.[/yellow] Job {job_id} keeps running on the server.\n"
            f"Reattach: [bold]axon jobs watch {job_id}[/bold]   "
            f"Cancel: [bold]axon jobs cancel {job_id}[/bold]"
        )
        raise typer.Exit(code=130) from None
    except ApiError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    if job.get("status") in {"error", "interrupted"}:
        console.print(f"[red]job failed:[/red] {job.get('error')}")
        raise typer.Exit(code=1)
    if job.get("status") == "cancelled":
        console.print("[yellow]job cancelled[/yellow]")
        raise typer.Exit(code=1)
    return job


def _client_or_exit(api_url: str | None) -> ApiClient:
    config = resolve_config(api_url)
    client = ApiClient(config)
    skew = client.version_skew()
    if skew:
        Console(stderr=True).print(f"[yellow]warning:[/yellow] {skew}")
    return client


@app.command("configure")
def configure_cmd(
    api_url: str | None = typer.Option(None, "--api-url", help="Axon API base URL"),
    token: str = typer.Option("", "--token", help="Bearer token (API_AUTH_MODE=dev)"),
) -> None:
    """Show or save where the Axon API is hosted (~/.axon/config.json)."""
    if api_url:
        path = save_config(api_url, token)
        console.print(f"[green]Saved[/green] api_url={api_url} to {path}")
    config = resolve_config()
    src = "configured" if config.configured else "default (not configured)"
    console.print(f"api_url: {config.api_url} ({src})")
    console.print(f"token: {'set' if config.token else 'not set'}")


@jobs_app.command("watch")
def jobs_watch_cmd(
    job_id: str,
    api_url: str | None = typer.Option(None, "--api-url"),
) -> None:
    """Reattach to a running index/scan job and follow its progress."""
    client = _client_or_exit(api_url)
    try:
        job = _watch_job(client, job_id, "job")
    finally:
        client.close()
    _print_job_result(job)


@jobs_app.command("cancel")
def jobs_cancel_cmd(
    job_id: str,
    api_url: str | None = typer.Option(None, "--api-url"),
) -> None:
    """Cancel a running index/scan job."""
    client = _client_or_exit(api_url)
    try:
        job = client.cancel_job(job_id)
    except ApiError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    finally:
        client.close()
    console.print(f"job {job_id}: cancel requested (status: {job.get('status')})")


def _print_job_result(job: dict) -> None:
    result = job.get("result") or {}
    if not result:
        return
    if "draft_yaml" in result:
        console.print(Panel(str(result["draft_yaml"]), title="draft axon.yaml"))
        for note in result.get("notes", []):
            console.print(f"[dim]{note}[/dim]")
        for amb in result.get("ambiguities", []):
            console.print(f"[yellow]review:[/yellow] {amb}")
        if result.get("written"):
            console.print("[green]Wrote axon.yaml[/green]")
        return
    if result.get("already_indexed"):
        console.print(
            f"[green]Already indexed[/green] at commit {str(result.get('commit'))[:12]} "
            "on the server; skipped (use --force to re-index)."
        )
    if (result.get("indexing") or {}).get("fallback_reason") == "indexer-changed":
        console.print("[yellow]axon upgraded since last index; re-indexing[/yellow]")
    services = ", ".join(str(s) for s in result.get("services", []))
    console.print(f"repo={result.get('repo')} services=[{services}]")
    console.print(f"nodes={result.get('nodes')} edges={result.get('edges')}")
    for item in (result.get("unresolved") or [])[:20]:
        console.print(f"[yellow]unresolved:[/yellow] {item}")
    _print_lost_links(result)
    if result.get("suppressed_calls"):
        console.print(f"suppressed non-operation calls: {result['suppressed_calls']}")
    if result.get("parse_warnings"):
        console.print(f"parse warnings: {result['parse_warnings']}")
        for sample in (result.get("parse_warning_samples") or [])[:5]:
            console.print(f"[dim]parse:[/dim] {sample}")


def _print_lost_links(result: dict) -> None:
    count = result.get("lost_cross_repo_link_count")
    if count:
        console.print(f"{count} cross-repo call site(s) lost their previous target")
    for item in result.get("lost_cross_repo_links", [])[:10]:
        console.print(
            f"{item['caller_repo']} {item['file']}:{item['line']} → {item['target_id']}",
            markup=False,
        )


def _load(repo: str | None, snapshot: str | None) -> GraphSnapshot:
    if snapshot:
        return load_snapshot_file(snapshot)
    if repo:
        snap, _ = index_repo(repo)
        return snap
    raise typer.BadParameter("pass --repo or --snapshot")


def _ai_draft_report(report):  # type: ignore[no-untyped-def]
    """Opt-in AI pass over a scan report (init-time only, never indexing)."""
    import asyncio

    from axon.config import get_settings
    from axon.v1.llm import client_from_settings
    from axon.v1.onboard_ai import ai_draft

    llm = client_from_settings(get_settings())
    if llm is None:
        console.print(
            "[red]--ai needs a configured model: set LLM_PROVIDER=local|external, "
            "LLM_BASE_URL and LLM_MODEL.[/red]"
        )
        raise typer.Exit(code=1)
    updated, _applied, _rejected = asyncio.run(ai_draft(report, llm))
    return updated


@app.command("init")
def init_cmd(
    repo: str = typer.Option(".", "--repo", help="Repository root to onboard"),
    write: bool = typer.Option(False, "--write", help="Write draft axon.yaml"),
    refresh: bool = typer.Option(
        False, "--refresh", help="Propose a diff preserving the existing service map"
    ),
    do_index: bool = typer.Option(False, "--index", help="Also index into the API graph"),
    api_url: str | None = typer.Option(None, "--api-url", help="Axon API base URL"),
    local: bool = typer.Option(False, "--local", help="Scan in this process, not via the API"),
    ai: bool = typer.Option(
        False,
        "--ai",
        help="Ask the configured LLM to draft client targets/specs (review before committing)",
    ),
) -> None:
    """Draft an axon.yaml service map (review it, then commit via PR).

    By default the scan runs as a job on the configured API server
    (see `axon configure`) with live progress. Use --local for an
    in-process scan. Add --index to load the graph right after.
    """
    from pathlib import Path

    root = Path(repo).resolve()
    if refresh:
        if write:
            console.print(
                "[red]--write refuses to overwrite an existing map; "
                "review the refresh diff manually.[/red]"
            )
            raise typer.Exit(code=1)
        report = refresh_repo(root)
        console.print(
            Panel(
                report.diff or "No service additions or removals.", title="proposed axon.yaml diff"
            )
        )
        for note in [*report.notes, *report.ambiguities]:
            console.print(f"[dim]{note}[/dim]")
        return
    if not local:
        client = _client_or_exit(api_url)
        try:
            try:
                job_id = client.create_scan_job(str(root), write=False)
            except ApiError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from None
            job = _watch_job(client, job_id, "scanning")
            report_dict = job.get("result") or {}
            if ai:
                from axon.v1.onboard import OnboardReport

                updated = _ai_draft_report(OnboardReport.model_validate(report_dict))
                report_dict = {
                    **report_dict,
                    "draft_yaml": updated.draft_yaml,
                    "notes": updated.notes,
                    "ambiguities": updated.ambiguities,
                }
            draft_yaml = str(report_dict.get("draft_yaml", ""))
            console.print(Panel(draft_yaml, title="draft axon.yaml"))
            for note in report_dict.get("notes", []):
                console.print(f"[dim]{note}[/dim]")
            for amb in report_dict.get("ambiguities", []):
                console.print(f"[yellow]review:[/yellow] {amb}")
            if write:
                target = root / "axon.yaml"
                if target.exists():
                    console.print("[red]axon.yaml already exists; refusing to overwrite.[/red]")
                    raise typer.Exit(code=1)
                target.write_text(draft_yaml, encoding="utf-8")
                console.print(f"[green]Wrote {target}[/green]")
            if do_index:
                _index_via_api(client, str(root), commit="", force=False)
            return
        finally:
            client.close()

    with _progress() as progress:
        task = progress.add_task("scanning", total=None)

        def on_scan(phase: str, done: int, total: int) -> None:
            progress.update(task, total=total or None, completed=done)

        report = scan_repo(repo, progress=on_scan)
    if ai:
        report = _ai_draft_report(report)
    console.print(Panel(report.draft_yaml, title="draft axon.yaml"))
    for note in report.notes:
        console.print(f"[dim]{note}[/dim]")
    for amb in report.ambiguities:
        console.print(f"[yellow]review:[/yellow] {amb}")
    if write:
        target = Path(repo) / "axon.yaml"
        if target.exists():
            console.print("[red]axon.yaml already exists; refusing to overwrite.[/red]")
            raise typer.Exit(code=1)
        target.write_text(report.draft_yaml, encoding="utf-8")
        console.print(f"[green]Wrote {target}[/green]")
    if do_index:
        client = _client_or_exit(api_url)
        try:
            _index_via_api(client, str(root), commit="", force=False)
        finally:
            client.close()


def _index_via_api(
    client: ApiClient,
    repo_path: str,
    commit: str,
    force: bool,
    services: list[str] | None = None,
    json_output: bool = False,
    api: str = "",
    replace_source: bool = False,
) -> None:
    try:
        job_id = client.create_index_job(
            repo_path,
            commit=commit,
            force=force,
            services=services,
            api=api,
            **({"replace_source": True} if replace_source else {}),
        )
        if json_output:
            job = client.poll_job(job_id, lambda *_: None)
            typer.echo(__import__("json").dumps(job.get("result") or job))
            if job.get("status") != "done":
                raise typer.Exit(code=2)
            return
    except ApiError as exc:
        if json_output:
            typer.echo(__import__("json").dumps({"error": str(exc)}))
            raise typer.Exit(code=2) from None
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[dim]index job {job_id} on {client.config.api_url}[/dim]")
    job = _watch_job(client, job_id, "indexing")
    _print_job_result(job)


@app.command("index")
def index_cmd(
    repo: str = typer.Option(..., "--repo", help="Repository root (needs axon.yaml)"),
    out: str | None = typer.Option(None, "--out", help="Write snapshot JSON here (local)"),
    store: str = typer.Option("memory", "--store", help="memory | neo4j (local store modes)"),
    commit: str = typer.Option("", "--commit"),
    timings_flag: bool = typer.Option(False, "--timings", help="Print per-phase timings"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass the facts cache (local)"),
    api_url: str | None = typer.Option(None, "--api-url", help="Axon API base URL"),
    local: bool = typer.Option(
        False, "--local", help="Index in this process instead of via the API server"
    ),
    force: bool = typer.Option(False, "--force", help="Re-index even if this commit is loaded"),
    services: list[str] | None = typer.Option(
        None, "--service", help="Fresh-parse named service (repeatable); publish whole repo"
    ),
    api: str = typer.Option(
        "",
        "--api",
        help="Fresh-parse one API only: endpoint id or 'METHOD /path' (API server mode)",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print structured index report"),
    replace_source: bool = typer.Option(
        False, "--replace-source", help="Reassign an existing repo ID to this checkout"
    ),
) -> None:
    """Index a repo into the API graph via the configured API server.

    The server does the work as a background job (live progress with
    ETA; Ctrl-C detaches without cancelling). Indexing the same
    commit twice is skipped server-side unless --force. --local (or
    --out / --store neo4j) indexes in this process instead.
    """
    from pathlib import Path

    if api and (local or out is not None or store == "neo4j"):
        typer.echo("--api needs API server mode (drop --local/--out/--store neo4j)")
        raise typer.Exit(code=2)
    if not local and out is None and store != "neo4j":
        client = _client_or_exit(api_url)
        try:
            _index_via_api(
                client,
                str(Path(repo).resolve()),
                commit=commit,
                force=force,
                services=services,
                json_output=json_output,
                api=api,
                replace_source=replace_source,
            )
        finally:
            client.close()
        return
    import dataclasses

    from axon.v1.indexer import IndexTimings

    timings = IndexTimings()
    from axon.v1.incremental import CheckoutChanged, index_and_publish
    from axon.v1.store import MemoryStore

    graph_store = (
        Neo4jStore()
        if store == "neo4j"
        else MemoryStore(load_snapshot_file(out) if out and Path(out).exists() else None)
    )
    if isinstance(graph_store, Neo4jStore):
        graph_store.ensure_schema()
    before = graph_store.load()
    try:
        snap, sm, indexing = index_and_publish(
            Path(repo),
            graph_store,
            commit=commit,
            timings=timings,
            services=services,
            force=force,
            replace_source=replace_source,
            use_cache=False if no_cache else None,
        )
    except (CheckoutChanged, ValueError) as exc:
        typer.echo(__import__("json").dumps({"error": str(exc)}) if json_output else str(exc))
        raise typer.Exit(code=2) from exc
    store_save_s = indexing["store_save_s"]
    if out:
        save_snapshot_file(graph_store.load(), out)
    selected = services or [svc.id for svc in sm.services]
    from axon.v1.scoped_index import focused_result

    # Use this call's committed repository view, never a later store publication.
    report = {**focused_result(before, snap, sm.repo, selected), **indexing}
    if json_output:
        typer.echo(__import__("json").dumps({"repo": sm.repo, **report}))
        return
    _print_lost_links(report)
    table = Table(title=f"Indexed {sm.repo}")
    table.add_column("Service")
    table.add_column("APIs", justify="right")
    table.add_column("With handler", justify="right")
    table.add_column("Out resolved", justify="right")
    table.add_column("Out external", justify="right")
    coverage = snap.coverage_by_service(sm.repo)
    for svc in sm.services:
        if svc.id not in selected:
            continue
        cov = coverage.get(
            svc.id,
            {"endpoints": 0, "with_handler": 0, "outbound_resolved": 0, "outbound_external": 0},
        )
        table.add_row(
            svc.id,
            str(cov["endpoints"]),
            str(cov["with_handler"]),
            str(cov["outbound_resolved"]),
            str(cov["outbound_external"]),
        )
    console.print(table)
    if services:
        typer.echo(__import__("json").dumps({"graph_delta": report["graph_delta"]}))
    for service, report in report["resolution"].items():
        console.print(f"{service}: handlers {report['handler_percent']}%", markup=False)
        if report["zero_endpoints"]:
            console.print(
                f"{service}: zero endpoints; check roots, specs and parser support", markup=False
            )
        for rule, count in sorted(
            report["unresolved_rules"].items(), key=lambda item: (-item[1], item[0])
        )[:5]:
            console.print(f"  {rule}: {count}", markup=False)
    console.print(
        f"nodes={len(snap.nodes)} edges={len(snap.edges)} unresolved={len(snap.unresolved)}"
    )
    if timings.suppressed_calls:
        console.print(f"suppressed non-operation calls: {timings.suppressed_calls}")
    if timings.parse_warnings:
        console.print(f"parse warnings: {timings.parse_warnings}")
        for sample in snap.diagnostics[:5]:
            console.print(f"[dim]parse:[/dim] {sample}")
    if timings_flag:
        for name, value in dataclasses.asdict(timings).items():
            if isinstance(value, float) and value > 0:
                console.print(f"{name}: {value:.2f}s")
            elif isinstance(value, int) and value:
                console.print(f"{name}: {value}")
        if store_save_s:
            console.print(f"store_save: {store_save_s:.2f}s")
    for item in snap.unresolved[:20]:
        console.print(f"[yellow]unresolved:[/yellow] {item}")


@app.command("doctor")
def doctor_cmd(
    repo: str = typer.Option(..., "--repo"),
    json_output: bool = typer.Option(False, "--json"),
    snapshot: str | None = typer.Option(
        None, "--snapshot", help="Include published graph diagnostics"
    ),
) -> None:
    """Check service roots, specs, client targets and unused declarations."""
    import json
    from pathlib import Path

    from axon.v1.doctor import doctor

    try:
        report = doctor(Path(repo), snapshot=load_snapshot_file(snapshot) if snapshot else None)
        from cli.commands.skills import installed_skill_issues

        report["issues"].extend(installed_skill_issues(Path(repo)))
    except (OSError, ValueError) as exc:
        if json_output:
            typer.echo(json.dumps({"error": str(exc)}))
            raise typer.Exit(1) from exc
        console.print(str(exc), markup=False)
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(json.dumps(report))
        if not report["ok"]:
            raise typer.Exit(1)
        return
    for suggestion in report.get("base_url_suggestions", []):
        console.print(suggestion, markup=False)
    for item in report["issues"]:
        console.print(f"{item['severity']} {item['code']}: {item['message']}", markup=False)
    console.print("Suggested ignores: " + ", ".join(report["suggested_ignores"]), markup=False)
    if not report["ok"]:
        raise typer.Exit(1)
    console.print("Setup checks passed")


@app.command("index-batch")
def index_batch_cmd(
    repos: list[str] = typer.Option(..., "--repo", help="Server-side checkout path (repeatable)"),
    api_url: str | None = typer.Option(None, "--api-url"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Publish repositories and validate their golden files together."""
    import json

    client = _client_or_exit(api_url)
    try:
        result = client.index_batch(repos)
    except ApiError as exc:
        typer.echo(json.dumps({"error": str(exc)}) if json_output else str(exc))
        raise typer.Exit(code=1) from exc
    finally:
        client.close()
    if json_output:
        typer.echo(json.dumps(result))
    else:
        console.print(f"Indexed {', '.join(result['repositories'])}", markup=False)
        _print_lost_links(result)


@app.command("verify")
def verify_cmd(
    repo: str | None = typer.Option(None, "--repo"),
    repo_id: str | None = typer.Option(None, "--repo-id", help="Verify in the shared server graph"),
    golden: str = typer.Option("axon.golden.yaml", "--golden"),
    snapshot: str | None = typer.Option(None, "--snapshot"),
    api_url: str | None = typer.Option(None, "--api-url"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Verify expected and forbidden outbound targets, with source snippets."""
    import json
    from pathlib import Path

    from axon.v1.golden import verify_golden

    try:
        if repo_id and not repo and not snapshot:
            client = _client_or_exit(api_url)
            try:
                report = client.verify_graph(repo_id, golden)
            finally:
                client.close()
        elif repo and not repo_id:
            root = Path(repo).resolve()
            snap = load_snapshot_file(snapshot) if snapshot else index_repo(root)[0]
            report = verify_golden(snap, root, root / golden)
        else:
            raise ValueError(
                "pass --repo for local verification or --repo-id for server verification"
            )
    except (ApiError, OSError, ValueError, KeyError, TypeError) as exc:
        if json_output:
            typer.echo(json.dumps({"error": str(exc)}))
            raise typer.Exit(1) from exc
        console.print(str(exc), markup=False)
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(json.dumps(report))
        if not report["ok"]:
            raise typer.Exit(1)
        return
    for case in report["cases"]:
        console.print(f"{'PASS' if case['ok'] else 'FAIL'} {case['id']}", markup=False)
        for line in [*case["snippets"], *case["errors"]]:
            console.print("  " + line, markup=False)
    if not report["ok"]:
        raise typer.Exit(1)


@app.command("verify-index")
def verify_index_cmd(
    repo: str = typer.Option(..., "--repo"),
    state_dir: str | None = typer.Option(None, "--state-dir"),
    snapshot: str | None = typer.Option(None, "--snapshot"),
) -> None:
    """Compare persisted index state with a fresh full rebuild, without publishing."""
    import json
    from pathlib import Path

    from axon.config import get_settings
    from axon.v1.incremental import audit_state
    from axon.v1.store import make_store

    graph = (
        load_snapshot_file(snapshot) if snapshot else make_store(get_settings().axon_store).load()
    )
    report = audit_state(Path(repo), state_dir, snapshot=graph)
    console.print(json.dumps(report, sort_keys=True), markup=False)
    if not report["ok"]:
        raise typer.Exit(1)


@app.command("upgrade-check")
def upgrade_check_cmd(
    repo: str | None = typer.Option(None, "--repo"),
    snapshot: str | None = typer.Option(None, "--snapshot"),
    state_dir: str | None = typer.Option(None, "--state-dir"),
    neo4j: bool = typer.Option(False, "--neo4j", help="Also inspect the configured Neo4j graph"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Read-only preflight: report stored versions without writing anything."""
    import json

    from axon.v1.upgrade import upgrade_check

    report = upgrade_check(repo, snapshot, neo4j=neo4j, state_dir=state_dir)
    if json_output:
        typer.echo(json.dumps(report, indent=1, sort_keys=True))
    else:
        console.print(
            f"axon {report['axon_version']} graph schema {report['graph_schema_version']}"
        )
        for name, check in report["checks"].items():
            console.print(f"{name}: {check.get('status')} {check.get('error', '')}".rstrip())
        console.print(f"verdict: {report['verdict']}")
    if report["verdict"] == "blocked":
        raise typer.Exit(1)


@app.command("graph-rollback")
def graph_rollback_cmd(
    repo: str | None = typer.Option(None, "--repo", help="Limit rollback to one repo id"),
    server: bool = typer.Option(False, "--server", help="Use the running Axon server"),
    api_url: str | None = typer.Option(None, "--api-url"),
) -> None:
    """Roll back through the server or directly on an offline Neo4j database."""
    import json

    try:
        if server or api_url:
            client = _client_or_exit(api_url)
            try:
                result = client.rollback_graph(repo)
            finally:
                client.close()
        else:
            result = {"rolled_back": Neo4jStore().rollback_generation(repo)}
    except Exception as exc:
        console.print(f"[red]{exc}[/red]", markup=False)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=1, sort_keys=True))


@app.command("snapshot-import")
def snapshot_import_cmd(
    snapshot: str = typer.Option(..., "--snapshot"),
    store_name: str = typer.Option("neo4j", "--store", help="neo4j (memory is per-process)"),
    server: bool = typer.Option(False, "--server", help="Use the running Axon server"),
    api_url: str | None = typer.Option(None, "--api-url"),
) -> None:
    """Import through the server or directly into an offline Neo4j database."""
    snap = load_snapshot_file(snapshot)
    if server or api_url:
        client = _client_or_exit(api_url)
        try:
            result = client.import_graph(snap.model_dump_json())
        except ApiError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc
        finally:
            client.close()
        console.print(f"Imported snapshot as publication {result['publication_id']}")
        return
    if store_name != "neo4j":
        typer.echo("snapshot-import publishes to Neo4j; use --snapshot reads for local work")
        raise typer.Exit(code=2)
    store = Neo4jStore()
    store.ensure_schema()
    store.save(snap)
    console.print(f"imported {len(snap.nodes)} nodes, {len(snap.edges)} edges from {snapshot}")


@app.command("graph-compare")
def graph_compare_cmd(
    before: str = typer.Option(..., "--before"),
    after: str = typer.Option(..., "--after"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Compare two snapshot files: counts, coverage, outbound and endpoint diffs."""
    import json

    from axon.v1.upgrade import compare_snapshots

    report = compare_snapshots(load_snapshot_file(before), load_snapshot_file(after))
    if json_output:
        typer.echo(json.dumps(report, indent=1, sort_keys=True))
        return
    console.print(f"nodes {report['nodes_before']} -> {report['nodes_after']}")
    console.print(f"edges {report['edges_before']} -> {report['edges_after']}")
    console.print(f"endpoints removed: {len(report['endpoints_removed'])}")
    for item in report["endpoints_removed"][:20]:
        console.print(f"  - {item}")
    console.print(
        f"outbound added {len(report['outbound_added'])} removed {len(report['outbound_removed'])}"
    )


def _consumer_mode(server: bool, local: bool, repo: str | None, snapshot: str | None) -> None:
    if server and (local or snapshot):
        raise typer.BadParameter("--server cannot be combined with --local or --snapshot")
    if server and repo:
        raise typer.BadParameter("server catalog/Ask use --repo-id for graph scope, not --repo")


@app.command("apis")
def apis_cmd(
    repo: str | None = typer.Option(None, "--repo"),
    snapshot: str | None = typer.Option(None, "--snapshot"),
    server: bool = typer.Option(False, "--server"),
    local: bool = typer.Option(False, "--local"),
    repo_id: str = typer.Option("", "--repo-id"),
    api_url: str | None = typer.Option(None, "--api-url"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List APIs from a local graph, combined snapshot, or server graph."""
    import json

    _consumer_mode(server, local, repo, snapshot)
    table = Table(title="APIs")
    for heading in ("Service", "Endpoint", "Handler", "Out", "In"):
        table.add_column(heading)
    if server:
        client = _client_or_exit(api_url)
        try:
            rows = client.list_apis(repo_id)
        except ApiError as exc:
            console.print(str(exc), markup=False)
            raise typer.Exit(1) from exc
        finally:
            client.close()
        for ep in rows:
            table.add_row(
                f"{ep['repo_id']}::{ep['service_id']}",
                f"{ep['props'].get('method', '')} {ep['props'].get('path', '')}",
                "—",
                "—",
                "—",
            )
    else:
        snap = _load(repo, snapshot)
        rows = [
            ep.model_dump(mode="json")
            for ep in snap.by_kind(NodeKind.API_ENDPOINT)
            if not repo_id or ep.repo_id == repo_id
        ]
        for ep_data in rows:
            ep = snap.nodes[ep_data["id"]]
            handler = snap.handler_of(ep.id)
            table.add_row(
                f"{ep.repo_id}::{ep.service_id}",
                f"{ep.props.get('method', '')} {ep.props.get('path', '')}",
                handler.props.get("qualified", "") if handler else "—",
                str(len(snap.outbound_endpoints(ep.id))),
                str(len(snap.inbound_callers(ep.id))),
            )
    if json_output:
        typer.echo(json.dumps(rows))
    else:
        console.print(table)


@app.command("ask")
def ask_cmd(
    question: str,
    repo: str | None = typer.Option(None, "--repo"),
    snapshot: str | None = typer.Option(None, "--snapshot"),
    server: bool = typer.Option(False, "--server"),
    local: bool = typer.Option(False, "--local"),
    api_url: str | None = typer.Option(None, "--api-url"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Ask the local graph, combined snapshot, or server graph."""
    _consumer_mode(server, local, repo, snapshot)
    if server:
        client = _client_or_exit(api_url)
        try:
            resp = ask_mod.AskResponse.model_validate(client.ask(question))
        except ApiError as exc:
            console.print(str(exc), markup=False)
            raise typer.Exit(1) from exc
        finally:
            client.close()
    else:
        resp = anyio.run(ask_mod.ask, _load(repo, snapshot), question)
    if json_output:
        typer.echo(resp.model_dump_json())
        return
    console.print(Panel(resp.answer, title=f"ask · {resp.intent}"))
    for c in resp.citations[:12]:
        console.print(f"[dim]cite:[/dim] {c.label}  {c.repo_id}::{c.service_id} {c.file}:{c.line}")


@app.command("chat")
def chat_cmd(
    repo: str | None = typer.Option(None, "--repo"),
    snapshot: str | None = typer.Option(None, "--snapshot"),
) -> None:
    """Interactive ask loop."""
    snap = _load(repo, snapshot)
    console.print(Panel("Ask about APIs. /exit to leave.", title="axon chat"))
    while True:
        text = Prompt.ask("[bold green]you[/bold green]").strip()
        if text in ("/exit", "exit", "quit"):
            return
        if not text:
            continue
        resp = anyio.run(ask_mod.ask, snap, text)
        console.print(Panel(resp.answer, title=resp.intent))


@app.command("pr-impact")
def pr_impact_cmd(
    repo: str = typer.Option(..., "--repo"),
    base: str = typer.Option(..., "--base", help="Base commit/branch"),
    head: str = typer.Option("HEAD", "--head"),
    fmt: str = typer.Option("markdown", "--format", help="markdown | json"),
    out: str | None = typer.Option(None, "--out"),
    fail_on_impact: bool = typer.Option(False, "--fail-on-impact"),
    server: bool = typer.Option(False, "--server"),
    repo_id: str = typer.Option("", "--repo-id"),
    local: bool = typer.Option(False, "--local"),
    snapshot: str | None = typer.Option(None, "--snapshot"),
    api_url: str | None = typer.Option(None, "--api-url"),
) -> None:
    """Analyze changed paths in a local, snapshot or published combined graph."""
    import subprocess

    from axon.v1.servicemap import load_service_map

    if server and (local or snapshot):
        raise typer.BadParameter("--server cannot be combined with --local or --snapshot")
    if server and not repo_id:
        raise typer.BadParameter("--server requires --repo-id")
    changed = git_changed_files(repo, base, head)
    selected_commit = subprocess.run(
        ["git", "rev-parse", head], cwd=repo, capture_output=True, text=True
    )
    expected_commit = selected_commit.stdout.strip() if selected_commit.returncode == 0 else ""
    if server:
        client = _client_or_exit(api_url)
        try:
            result = ImpactResult.model_validate(
                client.graph_impact(repo_id, changed, expected_commit)
            )
        except ApiError as exc:
            console.print(str(exc), markup=False)
            raise typer.Exit(1) from exc
        finally:
            client.close()
    else:
        snap = load_snapshot_file(snapshot) if snapshot else index_repo(repo)[0]
        identity = repo_id or load_service_map(repo).repo
        result = compute_impact(snap, changed, repo_id=identity)
        if expected_commit and result.indexed_commit and result.indexed_commit != expected_commit:
            result.limitations.append(
                f"Indexed revision {result.indexed_commit} "
                f"differs from selected head {expected_commit}."
            )
    text = result.model_dump_json(indent=1) if fmt == "json" else render_markdown(result)
    if out:
        from pathlib import Path

        Path(out).write_text(text, encoding="utf-8")
    console.print(text, markup=False)
    if fail_on_impact and result.affected_apis:
        raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
