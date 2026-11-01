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
from axon.v1.impact import compute_impact, git_changed_files, render_markdown
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
    return ApiClient(config)


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
    if result.get("suppressed_calls"):
        console.print(f"suppressed non-operation calls: {result['suppressed_calls']}")
    if result.get("parse_warnings"):
        console.print(f"parse warnings: {result['parse_warnings']}")
        for sample in (result.get("parse_warning_samples") or [])[:5]:
            console.print(f"[dim]parse:[/dim] {sample}")


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
) -> None:
    try:
        job_id = client.create_index_job(
            repo_path, commit=commit, force=force, **({"services": services} if services else {})
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
    json_output: bool = typer.Option(False, "--json", help="Print structured index report"),
) -> None:
    """Index a repo into the API graph via the configured API server.

    The server does the work as a background job (live progress with
    ETA; Ctrl-C detaches without cancelling). Indexing the same
    commit twice is skipped server-side unless --force. --local (or
    --out / --store neo4j) indexes in this process instead.
    """
    from pathlib import Path

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
            )
        finally:
            client.close()
        return
    import dataclasses
    import time

    from axon.v1.indexer import IndexTimings

    timings = IndexTimings()
    from axon.v1.graph import GraphSnapshot
    from axon.v1.scoped_index import evict_scoped_facts, focused_result, scoped_paths
    from axon.v1.servicemap import load_service_map

    try:
        fresh = scoped_paths(Path(repo), load_service_map(repo), services or [])
    except ValueError as exc:
        typer.echo(__import__("json").dumps({"error": str(exc)}) if json_output else str(exc))
        raise typer.Exit(code=2) from exc
    evict_scoped_facts(Path(repo), fresh)
    before = load_snapshot_file(out) if out and Path(out).exists() else GraphSnapshot()
    if store == "neo4j":
        before = Neo4jStore().load()
    snap, sm = index_repo(
        repo,
        force_parse_paths=fresh,
        commit=commit,
        timings=timings,
        use_cache=False if no_cache else None,
    )
    if store == "neo4j":
        neo = Neo4jStore()
        neo.ensure_schema()
        t_save = time.perf_counter()
        neo.save(snap)
        store_save_s = time.perf_counter() - t_save
    else:
        store_save_s = 0.0
    if out:
        save_snapshot_file(snap, out)
    selected = services or [svc.id for svc in sm.services]
    report = focused_result(before, snap, sm.repo, selected)
    if json_output:
        typer.echo(__import__("json").dumps({"repo": sm.repo, **report}))
        return
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
) -> None:
    """Check service roots, specs, client targets and unused declarations."""
    import json
    from pathlib import Path

    from axon.v1.doctor import doctor

    try:
        report = doctor(Path(repo))
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


@app.command("verify")
def verify_cmd(
    repo: str = typer.Option(..., "--repo"),
    golden: str = typer.Option("axon.golden.yaml", "--golden"),
    snapshot: str | None = typer.Option(None, "--snapshot"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Verify expected and forbidden outbound targets, with source snippets."""
    import json
    from pathlib import Path

    from axon.v1.golden import verify_golden

    root = Path(repo).resolve()
    try:
        snap = load_snapshot_file(snapshot) if snapshot else index_repo(root)[0]
        report = verify_golden(snap, root, root / golden)
    except (OSError, ValueError, KeyError, TypeError) as exc:
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


@app.command("apis")
def apis_cmd(
    repo: str | None = typer.Option(None, "--repo"),
    snapshot: str | None = typer.Option(None, "--snapshot"),
) -> None:
    """List APIs per service."""
    snap = _load(repo, snapshot)
    table = Table(title="APIs")
    table.add_column("Service")
    table.add_column("Endpoint")
    table.add_column("Handler")
    table.add_column("Out", justify="right")
    table.add_column("In", justify="right")
    for ep in snap.by_kind(NodeKind.API_ENDPOINT):
        handler = snap.handler_of(ep.id)
        table.add_row(
            ep.service_id,
            f"{ep.props.get('method', '')} {ep.props.get('path', '')}",
            handler.props.get("qualified", "") if handler else "—",
            str(len(snap.outbound_endpoints(ep.id))),
            str(len(snap.inbound_callers(ep.id))),
        )
    console.print(table)


@app.command("ask")
def ask_cmd(
    question: str,
    repo: str | None = typer.Option(None, "--repo"),
    snapshot: str | None = typer.Option(None, "--snapshot"),
) -> None:
    """Ask a natural-language question (graph-grounded)."""
    snap = _load(repo, snapshot)
    resp = anyio.run(ask_mod.ask, snap, question)
    console.print(Panel(resp.answer, title=f"ask · {resp.intent}"))
    for c in resp.citations[:12]:
        console.print(f"[dim]cite:[/dim] {c.label}  {c.file}:{c.line}")


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
) -> None:
    """List services + APIs affected by changes between base and head."""
    snap, sm = index_repo(repo)
    changed = git_changed_files(repo, base, head)
    result = compute_impact(snap, changed, repo_id=sm.repo)
    text = result.model_dump_json(indent=1) if fmt == "json" else render_markdown(result)
    if out:
        from pathlib import Path

        Path(out).write_text(text, encoding="utf-8")
    console.print(text)
    if fail_on_impact and result.affected_apis:
        raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
