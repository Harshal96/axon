"""/api/v2 router: per-API views, ask, index, impact, onboarding."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from axon.v2 import ask as ask_mod
from axon.v2.ask import AskResponse
from axon.v2.functions import (
    function_detail,
    owner_of_node,
    package_of,
    query_functions,
    service_key,
)
from axon.v2.graph import GraphSnapshot
from axon.v2.impact import compute_impact, render_markdown
from axon.v2.incremental import index_and_publish
from axon.v2.indexer import IndexCancelled, IndexTimings, index_repo
from axon.v2.jobs import JOBS
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
    base_commit: str = ""
    force: bool = False  # re-index even if this commit is already loaded
    services: list[str] = Field(default_factory=list)


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
        "handler_edges": [
            e.model_dump()
            for e in snap.edges
            if e.kind.value == "HANDLES" and e.to_id == endpoint_id
        ],
        "outbound": [_node_dict(t) for t in outbound],
        "callers": [_node_dict(c) for _, c in callers],
        "deprecatable": len(callers) == 0,
    }


@router.get("/functions")
def list_functions(
    request: Request,
    q: str = "",
    repo: str = "",
    used_in: list[str] = Query(default=[]),
    defined_in: list[str] = Query(default=[]),
    kind: list[str] = Query(default=[]),
    hide_handlers: bool = True,
    unreached: bool = False,
    sort: Literal["usage", "apis", "callers", "name"] = "usage",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    return query_functions(
        _snap(request),
        q=q,
        repo=repo,
        used_in=used_in,
        defined_in=defined_in,
        kinds=kind,
        hide_handlers=hide_handlers,
        unreached=unreached,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@router.get("/functions/apis")
def function_apis(
    request: Request,
    q: str,
    repo: str = "",
    used_in: list[str] = Query(default=[]),
    defined_in: list[str] = Query(default=[]),
) -> list[dict[str, Any]]:
    snap = _snap(request)
    out = []
    for sym in snap.find_symbols(q):
        if repo and sym.repo_id != repo:
            continue
        owner = owner_of_node(sym)
        if defined_in and owner["key"] not in defined_in:
            continue
        apis = snap.endpoint_of_symbol(sym.id)
        if repo:
            apis = [ep for ep in apis if ep.repo_id == repo]
        if used_in:
            apis = [ep for ep in apis if service_key(ep) in used_in]
            if not apis:
                continue
        out.append(
            {
                "symbol": _node_dict(sym),
                "apis": [_node_dict(a) for a in apis],
                "owner": owner,
                "package": package_of(sym.file, sym.props.get("language", "")),
            }
        )
        if len(out) == 20:
            break
    return out


@router.get("/functions/{symbol_id:path}")
def function_inspector(request: Request, symbol_id: str) -> dict[str, Any]:
    try:
        return function_detail(_snap(request), symbol_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="symbol not found") from exc


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
    from axon.v2.revision import checkout_dirty
    from axon.v2.servicemap import service_map_fingerprint

    dirty = checkout_dirty(root)
    if dirty is not False:
        return None
    sm = load_service_map(root)
    snap = store.load()
    node = snap.nodes.get(repo_node_id(sm.repo))
    from axon.v2.fingerprints import INDEXER_FINGERPRINT

    if node is None or node.props.get("axon_build") != INDEXER_FINGERPRINT:
        return None
    if node.props.get("commit") != commit:
        return None
    if node.props.get("service_map_hash") != service_map_fingerprint(root):
        return None
    if node.props.get("graph_unsound") == "true" or node.props.get("file_degraded", "0") != "0":
        return None
    if node.props.get("dirty") != "false":
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
        "resolution": snap.resolution_report(repo),
    }


@router.post("/index")
def index_endpoint(request: Request, body: IndexRequest) -> dict[str, Any]:
    root = Path(body.repo_path)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="repo_path is not a directory")
    timings = IndexTimings()
    commit = _resolve_commit(root, body.commit)
    try:
        if not body.force and not body.services:
            hit = _already_indexed(_store(request), root, commit)
            if hit is not None:
                return {**hit, "timings": dataclasses.asdict(timings)}
        with JOBS.index_lock(str(root), load_service_map(root).repo):
            snap, sm, indexing = index_and_publish(
                root,
                _store(request),
                commit=commit,
                services=body.services,
                base_commit=body.base_commit,
                force=body.force,
                timings=timings,
            )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = _index_result(snap, sm.repo, [s.id for s in sm.services])
    if body.services:
        result.update(
            {key: indexing[key] for key in ("services", "coverage", "resolution", "graph_delta")}
        )
    return {
        "indexing": indexing,
        "warnings": indexing.get("warnings", []),
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
    from axon.v2.scoped_index import scoped_paths

    try:
        scoped_paths(root, load_service_map(root), body.services)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store = _store(request)
    job = await asyncio.to_thread(JOBS.create, body.repo_path, repo_id=load_service_map(root).repo)
    cancel_event = await asyncio.to_thread(JOBS.cancel_event, job.id) or threading.Event()
    last_phase = [""]
    last_done = [-1]

    def progress(phase: str, done: int, total: int) -> None:
        if phase != last_phase[0] or done - last_done[0] >= 25 or done == total:
            last_phase[0] = phase
            last_done[0] = done
            JOBS.update(job.id, phase=phase, done=done, total=total)

    def run() -> None:
        run_lock = JOBS.index_lock(str(root), job.repo_id)
        while not run_lock.acquire(timeout=0.25):
            if cancel_event.is_set():
                JOBS.update(job.id, status="cancelled")
                return
        try:
            if cancel_event.is_set():
                JOBS.update(job.id, status="cancelled")
                return
            JOBS.update(job.id, status="running")
            timings = IndexTimings()
            commit = _resolve_commit(root, body.commit)
            if not body.force and not body.services:
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
            snap, sm, indexing = index_and_publish(
                root,
                store,
                services=body.services,
                base_commit=body.base_commit,
                force=body.force,
                commit=commit,
                timings=timings,
                progress=progress,
                cancelled=cancel_event,
            )
            result = _index_result(snap, sm.repo, [s.id for s in sm.services])
            if body.services:
                result.update(
                    {
                        key: indexing[key]
                        for key in ("services", "coverage", "resolution", "graph_delta")
                    }
                )
            result["suppressed_calls"] = timings.suppressed_calls
            result["parse_warnings"] = timings.parse_warnings
            result["parse_warning_samples"] = snap.diagnostics[:10]
            result["commit"] = commit
            result["indexing"] = indexing
            result["warnings"] = indexing.get("warnings", [])
            JOBS.update(
                job.id,
                status="done",
                phase="done",
                result=result,
                timings=dataclasses.asdict(timings),
            )
        except IndexCancelled:
            JOBS.update(job.id, status="cancelled")
        except Exception as exc:  # surfaced via job status, not a 500 mid-run
            JOBS.update(job.id, status="error", error=str(exc))
        finally:
            run_lock.release()

    loop = asyncio.get_running_loop()
    task = loop.run_in_executor(None, run)
    asyncio.ensure_future(asyncio.wrap_future(task))
    return JSONResponse(status_code=202, content={"job_id": job.id, "status": job.status})


@router.get("/index/jobs/{job_id}")
async def index_job_get(job_id: str) -> dict[str, Any]:
    job = await asyncio.to_thread(JOBS.snapshot, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job id")
    return job


@router.post("/index/jobs/{job_id}/cancel")
async def index_job_cancel(job_id: str) -> dict[str, Any]:
    job = await asyncio.to_thread(JOBS.snapshot, job_id, cancel=True)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job id")
    return job


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
    if body.write:
        raise HTTPException(
            status_code=400, detail="server never writes config; use axon init --write locally"
        )
    root = Path(body.repo_path)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="repo_path is not a directory")
    report = scan_repo(root)
    return {**report.model_dump(), "written": False}


@router.post("/onboard/scan/jobs", status_code=202)
async def onboard_scan_job_create(body: OnboardRequest) -> JSONResponse:
    if body.write:
        raise HTTPException(
            status_code=400, detail="server never writes config; use axon init --write locally"
        )
    root = Path(body.repo_path)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="repo_path is not a directory")
    job = await asyncio.to_thread(JOBS.create, body.repo_path, kind="scan")
    cancel_event = await asyncio.to_thread(JOBS.cancel_event, job.id) or threading.Event()

    def progress(phase: str, done: int, total: int) -> None:
        JOBS.update(job.id, phase=phase, done=done, total=total)

    def run() -> None:
        try:
            if cancel_event.is_set():
                JOBS.update(job.id, status="cancelled")
                return
            JOBS.update(job.id, status="running")
            report = scan_repo(root, progress=progress, cancelled=cancel_event)
            JOBS.update(
                job.id,
                status="done",
                phase="done",
                result={**report.model_dump(), "written": False},
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
    job = await asyncio.to_thread(JOBS.snapshot, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job id")
    return job


@router.post("/jobs/{job_id}/cancel")
async def job_cancel(job_id: str) -> dict[str, Any]:
    job = await asyncio.to_thread(JOBS.snapshot, job_id, cancel=True)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job id")
    return job


@router.get("/repos/{repo}/api-explain")
def maintenance_api_explain(
    request: Request, repo: str, api: str, service: str = ""
) -> dict[str, Any]:
    from axon.v2.diagnose import api_explain

    try:
        return api_explain(_snap(request), repo, api, service)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/repos/{repo}/clients")
def maintenance_clients(request: Request, repo: str) -> dict[str, Any]:
    from axon.v2.diagnose import client_list

    return {"clients": client_list(_snap(request), repo)}


@router.get("/repos/{repo}/clients/{client}")
def maintenance_client(request: Request, repo: str, client: str) -> dict[str, Any]:
    from axon.v2.diagnose import client_show

    try:
        return client_show(_snap(request), repo, client)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/repos/{repo}/services/{service}")
def maintenance_service(request: Request, repo: str, service: str) -> dict[str, Any]:
    from axon.v2.diagnose import service_show

    try:
        return service_show(_snap(request), repo, service)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/repos/{repo}/refs/{kind}/{entity:path}")
def maintenance_refs(request: Request, repo: str, kind: str, entity: str) -> dict[str, Any]:
    from axon.v2.diagnose import graph_refs

    try:
        return graph_refs(_snap(request), repo, kind, entity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
