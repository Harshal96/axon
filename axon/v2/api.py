"""/api/v2 router: per-API views, ask, index, impact, onboarding."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from axon.v2 import ask as ask_mod
from axon.v2.ask import AskResponse
from axon.v2.graph import GraphSnapshot
from axon.v2.impact import compute_impact, render_markdown
from axon.v2.indexer import IndexCancelled, IndexTimings, index_repo
from axon.v2.jobs import JOBS, RUN_LOCK
from axon.v2.llm import client_from_settings
from axon.v2.models import GraphNode, NodeKind, repo_node_id
from axon.v2.onboard import ScanCancelled, scan_repo
from axon.v2.servicemap import load_service_map
from axon.v2.store import GraphStore

logger = logging.getLogger(__name__)
router = APIRouter()


def _store(request: Request) -> GraphStore:
    store: GraphStore = request.app.state.v2_store
    return store


def _snap(request: Request) -> GraphSnapshot:
    return _store(request).load()


def _node_dict(n: GraphNode) -> dict[str, Any]:
    d: dict[str, Any] = n.model_dump(mode="json")
    d["label"] = (
        f"{n.props.get('method', '')} {n.props.get('path', '')}".strip()
        if n.kind == NodeKind.API_ENDPOINT
        else (n.props.get("qualified") or n.name)
    )
    return d


class AskRequest(BaseModel):
    question: str
    engine: str = "auto"  # auto | deterministic | model (ask.ask)


class IndexRequest(BaseModel):
    repo_path: str
    commit: str = ""
    force: bool = False  # re-index even if this commit is already loaded


class ImpactRequest(BaseModel):
    repo_path: str
    changed_files: list[str]


class OnboardRequest(BaseModel):
    repo_path: str
    write: bool = False


@router.get("/repos")
def list_repos(request: Request) -> list[dict[str, Any]]:
    return [_node_dict(n) for n in _snap(request).by_kind(NodeKind.REPO)]


@router.get("/services")
def list_services(request: Request, repo: str = "") -> list[dict[str, Any]]:
    snap = _snap(request)
    out = []
    for svc in snap.by_kind(NodeKind.SERVICE):
        if repo and svc.repo_id != repo:
            continue
        d = _node_dict(svc)
        d["api_count"] = sum(
            1
            for e in snap.by_kind(NodeKind.API_ENDPOINT)
            if e.service_id == svc.service_id and e.repo_id == svc.repo_id
        )
        out.append(d)
    return out


@router.get("/apis")
def list_apis(
    request: Request, service: str = "", repo: str = "", q: str = ""
) -> list[dict[str, Any]]:
    snap = _snap(request)
    eps = snap.by_kind(NodeKind.API_ENDPOINT)
    if service:
        eps = [e for e in eps if e.service_id == service]
    if repo:
        eps = [e for e in eps if e.repo_id == repo]
    if q:
        ql = q.lower()
        eps = [
            e
            for e in eps
            if ql
            in (
                e.props.get("path", "") + " " + e.props.get("operation_id", "") + " " + e.service_id
            ).lower()
        ]
    return [_node_dict(e) for e in eps]


@router.get("/apis/{endpoint_id:path}/graph")
def api_graph(
    request: Request,
    endpoint_id: str,
    depth: int = 2,
    max_symbols: int = 80,
    max_callers: int = 50,
    max_owners_per_caller: int = 4,
) -> dict[str, Any]:
    snap = _snap(request)
    if endpoint_id not in snap.nodes:
        raise HTTPException(status_code=404, detail="endpoint not found")
    return snap.api_graph(
        endpoint_id,
        depth=max(1, min(depth, 5)),
        max_symbols=max_symbols,
        max_callers=max_callers,
        max_owners_per_caller=max_owners_per_caller,
    )


@router.get("/apis/{endpoint_id:path}")
def api_detail(request: Request, endpoint_id: str) -> dict[str, Any]:
    snap = _snap(request)
    ep = snap.nodes.get(endpoint_id)
    if ep is None or ep.kind != NodeKind.API_ENDPOINT:
        raise HTTPException(status_code=404, detail="endpoint not found")
    handler = snap.handler_of(endpoint_id)
    outbound = snap.outbound_endpoints(endpoint_id)
    callers = snap.inbound_callers(endpoint_id)
    return {
        "endpoint": _node_dict(ep),
        "handler": _node_dict(handler) if handler else None,
        "handlers": [_node_dict(h) for h in snap.handlers_of(endpoint_id)],
        "outbound": [_node_dict(t) for t in outbound],
        "callers": [_node_dict(c) for _, c in callers],
        "deprecatable": len(callers) == 0,
    }


@router.get("/functions/apis")
def function_apis(request: Request, q: str) -> list[dict[str, Any]]:
    snap = _snap(request)
    out = []
    for sym in snap.find_symbols(q)[:20]:
        apis = snap.endpoint_of_symbol(sym.id)
        out.append({"symbol": _node_dict(sym), "apis": [_node_dict(a) for a in apis]})
    return out


@router.get("/search")
def search(request: Request, q: str, limit: int = 20) -> list[dict[str, Any]]:
    snap = _snap(request)
    ql = q.lower()
    hits = []
    for n in snap.nodes.values():
        hay = f"{n.name} {n.props.get('qualified', '')} {n.props.get('path', '')} {n.id}".lower()
        if ql in hay:
            hits.append(_node_dict(n))
    hits.sort(key=lambda d: (d["kind"] != "ApiEndpoint", d["id"]))
    return hits[: max(1, min(limit, 100))]


@router.post("/ask")
async def ask_endpoint(request: Request, body: AskRequest) -> AskResponse:
    from axon.config import get_settings

    store = _store(request)
    llm = client_from_settings(get_settings())
    # First load can take minutes on a large graph; keep it off the loop.
    snap = await asyncio.to_thread(store.load)
    return await ask_mod.ask(snap, body.question, llm=llm, store=store, engine=body.engine)


def _resolve_commit(root: Path, given: str) -> str:
    """Explicit commit wins; otherwise the repo's git HEAD, best-effort."""
    if given:
        return given
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _already_indexed(store: GraphStore, root: Path, commit: str) -> dict[str, Any] | None:
    """Result payload if this exact commit is already in the store."""
    if not commit:
        return None
    sm = load_service_map(root)
    snap = store.load()
    node = snap.nodes.get(repo_node_id(sm.repo))
    if node is None or node.props.get("commit") != commit:
        return None
    if node.props.get("index_options") != sm.index.fingerprint():
        return None  # scope options changed (or predate stamping): re-index
    result = _index_result(snap, sm.repo, [s.id for s in sm.services])
    return {**result, "already_indexed": True, "commit": commit}


def _index_result(snap: GraphSnapshot, repo: str, services: list[str]) -> dict[str, Any]:
    return {
        "repo": repo,
        "services": services,
        "nodes": len(snap.nodes),
        "edges": len(snap.edges),
        "unresolved": snap.unresolved[:50],
        "coverage": snap.coverage_by_service(repo),
    }


@router.post("/index")
def index_endpoint(request: Request, body: IndexRequest) -> dict[str, Any]:
    root = Path(body.repo_path)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="repo_path is not a directory")
    timings = IndexTimings()
    commit = _resolve_commit(root, body.commit)
    try:
        if not body.force:
            hit = _already_indexed(_store(request), root, commit)
            if hit is not None:
                return {**hit, "timings": dataclasses.asdict(timings)}
        snap, sm = index_repo(root, commit=commit, timings=timings)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = _index_result(snap, sm.repo, [s.id for s in sm.services])
    _store(request).save(snap)
    return {
        **result,
        "timings": dataclasses.asdict(timings),
        "parse_warnings": timings.parse_warnings,
        "suppressed_calls": timings.suppressed_calls,
        "parse_warning_samples": snap.diagnostics[:10],
    }


@router.post("/index/jobs", status_code=202)
async def index_job_create(request: Request, body: IndexRequest) -> JSONResponse:
    root = Path(body.repo_path)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="repo_path is not a directory")
    store = _store(request)
    job = JOBS.create(body.repo_path)
    cancel_event = JOBS.cancel_event(job.id) or threading.Event()
    last_phase = [""]
    last_done = [-1]

    def progress(phase: str, done: int, total: int) -> None:
        if phase != last_phase[0] or done - last_done[0] >= 25 or done == total:
            last_phase[0] = phase
            last_done[0] = done
            JOBS.update(job.id, phase=phase, done=done, total=total)

    def run() -> None:
        while not RUN_LOCK.acquire(timeout=0.25):
            if cancel_event.is_set():
                JOBS.update(job.id, status="cancelled")
                return
        try:
            JOBS.update(job.id, status="running")
            timings = IndexTimings()
            commit = _resolve_commit(root, body.commit)
            if not body.force:
                hit = _already_indexed(store, root, commit)
                if hit is not None:
                    JOBS.update(
                        job.id,
                        status="done",
                        phase="done",
                        result=hit,
                        timings=dataclasses.asdict(timings),
                    )
                    return
            snap, sm = index_repo(
                root,
                commit=commit,
                timings=timings,
                progress=progress,
                cancelled=cancel_event,
            )
            result = _index_result(snap, sm.repo, [s.id for s in sm.services])
            result["suppressed_calls"] = timings.suppressed_calls
            result["parse_warnings"] = timings.parse_warnings
            result["parse_warning_samples"] = snap.diagnostics[:10]
            result["commit"] = commit
            store.save(snap)
            JOBS.update(job.id, status="done", result=result, timings=dataclasses.asdict(timings))
        except IndexCancelled:
            JOBS.update(job.id, status="cancelled")
        except Exception as exc:  # surfaced via job status, not a 500 mid-run
            JOBS.update(job.id, status="error", error=str(exc))
        finally:
            RUN_LOCK.release()

    loop = asyncio.get_running_loop()
    task = loop.run_in_executor(None, run)
    asyncio.ensure_future(asyncio.wrap_future(task))
    return JSONResponse(status_code=202, content={"job_id": job.id, "status": job.status})


@router.get("/index/jobs/{job_id}")
async def index_job_get(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job id")
    return job.to_dict()


@router.post("/index/jobs/{job_id}/cancel")
async def index_job_cancel(job_id: str) -> dict[str, Any]:
    job = JOBS.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job id")
    return job.to_dict()


@router.post("/impact")
def impact_endpoint(body: ImpactRequest) -> dict[str, Any]:
    root = Path(body.repo_path)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="repo_path is not a directory")
    try:
        snap, sm = index_repo(root)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = compute_impact(snap, body.changed_files, repo_id=sm.repo)
    return {**result.model_dump(), "markdown": render_markdown(result)}


@router.post("/onboard/scan")
def onboard_endpoint(body: OnboardRequest) -> dict[str, Any]:
    root = Path(body.repo_path)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="repo_path is not a directory")
    report = scan_repo(root)
    written = False
    if body.write:
        target = root / "axon.yaml"
        if target.exists():
            raise HTTPException(
                status_code=409, detail="axon.yaml already exists; refusing to overwrite"
            )
        target.write_text(report.draft_yaml, encoding="utf-8")
        written = True
    return {**report.model_dump(), "written": written}


@router.post("/onboard/scan/jobs", status_code=202)
async def onboard_scan_job_create(body: OnboardRequest) -> JSONResponse:
    root = Path(body.repo_path)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="repo_path is not a directory")
    job = JOBS.create(body.repo_path, kind="scan")
    cancel_event = JOBS.cancel_event(job.id) or threading.Event()

    def progress(phase: str, done: int, total: int) -> None:
        JOBS.update(job.id, phase=phase, done=done, total=total)

    def run() -> None:
        try:
            JOBS.update(job.id, status="running")
            report = scan_repo(root, progress=progress, cancelled=cancel_event)
            written = False
            if body.write:
                target = root / "axon.yaml"
                if target.exists():
                    raise FileExistsError("axon.yaml already exists; refusing to overwrite")
                target.write_text(report.draft_yaml, encoding="utf-8")
                written = True
            JOBS.update(
                job.id,
                status="done",
                phase="done",
                result={**report.model_dump(), "written": written},
            )
        except ScanCancelled:
            JOBS.update(job.id, status="cancelled")
        except Exception as exc:  # surfaced via job status, not a 500 mid-run
            JOBS.update(job.id, status="error", error=str(exc))

    loop = asyncio.get_running_loop()
    task = loop.run_in_executor(None, run)
    asyncio.ensure_future(asyncio.wrap_future(task))
    return JSONResponse(status_code=202, content={"job_id": job.id, "status": job.status})


@router.get("/jobs/{job_id}")
async def job_get(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job id")
    return job.to_dict()


@router.post("/jobs/{job_id}/cancel")
async def job_cancel(job_id: str) -> dict[str, Any]:
    job = JOBS.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job id")
    return job.to_dict()
