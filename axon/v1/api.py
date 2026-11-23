"""/api/v1 router: per-API views, ask, index, impact, onboarding."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import threading
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from axon.v1 import ask as ask_mod
from axon.v1.api_fix import CorrectionRequest
from axon.v1.ask import AskResponse
from axon.v1.functions import (
    function_detail,
    owner_of_node,
    package_of,
    query_functions,
    service_key,
)
from axon.v1.graph import GraphSnapshot
from axon.v1.impact import compute_impact, render_markdown
from axon.v1.incremental import (
    CheckoutChanged,
    SourcePathConflict,
    check_source_identity,
    index_and_publish,
    publication_result,
)
from axon.v1.indexer import IndexCancelled, IndexTimings, index_repo
from axon.v1.jobs import JOBS
from axon.v1.llm import client_from_settings
from axon.v1.models import GraphNode, NodeKind, repo_node_id
from axon.v1.onboard import ScanCancelled, scan_repo
from axon.v1.revision import resolve_index_commit
from axon.v1.servicemap import load_service_map
from axon.v1.store import GraphStore

logger = logging.getLogger(__name__)
router = APIRouter()


def _store(request: Request) -> GraphStore:
    store: GraphStore = request.app.state.v1_store
    return store


def _snap(request: Request) -> GraphSnapshot:
    if not hasattr(request.state, "graph_snapshot"):
        request.state.graph_snapshot = _store(request).load()
        request.state.publication_id = request.state.graph_snapshot.publication_id
    snapshot: GraphSnapshot = request.state.graph_snapshot
    return snapshot


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
    replace_source: bool = False
    services: list[str] = Field(default_factory=list)
    api: str = ""  # endpoint id or "METHOD /path": fresh-parse only its files


class ImpactRequest(BaseModel):
    repo_path: str
    changed_files: list[str]


class GraphImpactRequest(BaseModel):
    repo_id: str = Field(min_length=1)
    changed_files: list[str]
    expected_commit: str = ""


class OnboardRequest(BaseModel):
    repo_path: str
    write: bool = False


class BatchIndexRequest(BaseModel):
    repo_paths: list[str] = Field(min_length=1)


class GraphVerifyRequest(BaseModel):
    repo_id: str = Field(min_length=1)
    golden: str = "axon.golden.yaml"


@router.post("/index/batch")
def index_batch(request: Request, body: BatchIndexRequest) -> dict[str, Any]:
    """Index targets with interdependent contracts as a single publication."""
    from axon.v1.batch_index import publish_batch

    try:
        committed, repositories, link_losses = publish_batch(_store(request), body.repo_paths)
    except SourcePathConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request.state.publication_id = committed.publication_id
    return {
        **publication_result(committed),
        "repositories": repositories,
        "lost_cross_repo_links": link_losses[:50],
        "lost_cross_repo_link_count": len(link_losses),
    }


@router.post("/graph/verify")
def verify_graph(request: Request, body: GraphVerifyRequest) -> dict[str, Any]:
    from axon.v1.golden import verify_golden

    snapshot = _snap(request)
    repo = snapshot.nodes.get(repo_node_id(body.repo_id))
    if repo is None:
        raise HTTPException(status_code=404, detail="unknown repository")
    source = repo.props.get("source_path")
    if not source:
        raise HTTPException(status_code=400, detail="repository has no recorded checkout")
    root = Path(source).resolve()
    golden = (root / body.golden).resolve()
    if not golden.is_relative_to(root):
        raise HTTPException(status_code=400, detail="golden file must be inside the checkout")
    try:
        if load_service_map(root).repo != body.repo_id:
            raise SourcePathConflict("checkout identity changed; re-index the repository")
        verification = verify_golden(snapshot, root, golden)
    except SourcePathConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**verification, "publication_id": snapshot.publication_id}


class GraphRollbackRequest(BaseModel):
    repo_id: str | None = None


@router.post("/graph/rollback")
def rollback_graph(request: Request, body: GraphRollbackRequest) -> dict[str, Any]:
    from axon.v1.store import Neo4jStore

    store = _store(request)
    if not isinstance(store, Neo4jStore):
        raise HTTPException(status_code=400, detail="rollback requires a Neo4j store")
    try:
        rows, publication_id = store.rollback_publication(body.repo_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    request.state.publication_id = publication_id
    return {"rolled_back": rows, "publication_id": publication_id}


@router.post("/graph/import")
def import_graph(request: Request, body: GraphSnapshot) -> dict[str, Any]:
    try:
        committed = _store(request).publish_repositories(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request.state.publication_id = committed.publication_id
    return {
        "publication_id": committed.publication_id,
        "nodes": len(body.nodes),
        "edges": len(body.edges),
    }


@router.get("/repos")
def list_repos(request: Request) -> list[dict[str, Any]]:
    return [_node_dict(n) for n in _snap(request).by_kind(NodeKind.REPO)]


@router.delete("/repos/{repo_id:path}")
def remove_repo(request: Request, repo_id: str) -> dict[str, Any]:
    """Remove a graph ownership scope and replay retained callers; no checkout IO."""
    store = _store(request)
    with JOBS.index_lock("", repo_id):
        if repo_node_id(repo_id) not in store.load().nodes:
            raise HTTPException(status_code=404, detail="unknown repository")
        committed = store.remove_repository(repo_id)
        JOBS.cancel_queued_indexes(repo_id)
    return {
        "repo": repo_id,
        "publication_id": committed.publication_id,
        "affected_callers": {
            repo: dataclasses.asdict(report) for repo, report in committed.link_reports.items()
        },
    }


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
@router.get("/api-graph")
def api_graph(
    request: Request,
    endpoint_id: str,
    depth: int = 2,
    max_symbols: int = 80,
    max_callers: int = 50,
    max_owners_per_caller: int = 4,
) -> dict[str, Any]:
    _endpoint_or_404(request, endpoint_id)
    snap = _snap(request)
    return {
        "publication_id": snap.publication_id,
        **snap.api_graph(
            endpoint_id,
            depth=max(1, min(depth, 5)),
            max_symbols=max_symbols,
            max_callers=max_callers,
            max_owners_per_caller=max_owners_per_caller,
        ),
    }


@router.get("/apis/{endpoint_id:path}")
def api_detail(request: Request, endpoint_id: str) -> dict[str, Any]:
    from axon.v1.diagnose import link_diagnosis

    ep = _endpoint_or_404(request, endpoint_id)
    snap = _snap(request)
    handler = snap.handler_of(endpoint_id)
    source = snap.nodes.get(repo_node_id(handler.repo_id)) if handler else None
    outbound = snap.outbound_calls(endpoint_id, all_sites=True)
    callers = snap.inbound_callers(endpoint_id)
    return {
        "publication_id": snap.publication_id,
        "endpoint": _node_dict(ep),
        "handler": _node_dict(handler) if handler else None,
        "handler_source_path": source.props.get("source_path", "") if source else "",
        "handlers": [_node_dict(h) for h in snap.handlers_of(endpoint_id)],
        "handler_edges": [
            e.model_dump()
            for e in snap.edges
            if e.kind.value == "HANDLES" and e.to_id == endpoint_id
        ],
        "outbound": [_node_dict(t) for t in {t.id: t for _, t in outbound}.values()],
        "outbound_sites": [
            {
                **edge.model_dump(mode="json"),
                **(link_diagnosis(snap, edge) if target.kind == NodeKind.EXTERNAL_API else {}),
            }
            for edge, target in outbound
        ],
        "callers": [_node_dict(c) for c in {c.id: c for _, c in callers}.values()],
        "caller_sites": [edge.model_dump(mode="json") for edge, _ in callers],
        "deprecatable": len(callers) == 0,
        "correction_llm_configured": _llm_configured(),
    }


@router.get("/api-view")
def api_view(request: Request, endpoint_id: str) -> dict[str, Any]:
    """Read detail and graph from one request-scoped publication."""
    return {
        "detail": api_detail(request, endpoint_id),
        "graph": api_graph(request, endpoint_id),
    }


def _llm_configured() -> bool:
    try:
        from axon.config import get_settings

        return client_from_settings(get_settings()) is not None
    except Exception:
        return False


def _endpoint_or_404(request: Request, endpoint_id: str) -> GraphNode:
    ep = _snap(request).nodes.get(endpoint_id)
    if ep is None or ep.kind != NodeKind.API_ENDPOINT:
        raise HTTPException(status_code=404, detail="endpoint not found")
    return ep


def _repo_root(request: Request, endpoint_id: str) -> Path:
    snap = _snap(request)
    ep = _endpoint_or_404(request, endpoint_id)
    repo_node = snap.nodes.get(repo_node_id(ep.repo_id))
    source = repo_node.props.get("source_path", "") if repo_node else ""
    root = Path(source) if source else Path()
    if not source or not root.is_dir():
        raise HTTPException(
            status_code=400,
            detail="repo checkout path unknown for this API; re-index with repo_path first",
        )
    try:
        if load_service_map(root).repo != ep.repo_id:
            raise HTTPException(status_code=409, detail="checkout identity changed; re-index it")
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return root


@router.post("/apis/{endpoint_id:path}/recheck", status_code=202)
async def api_recheck(request: Request, endpoint_id: str) -> JSONResponse:
    """Fresh-parse only this API's files and republish the repo graph."""
    root = _repo_root(request, endpoint_id)
    return await index_job_create(request, IndexRequest(repo_path=str(root), api=endpoint_id))


@router.post("/apis/{endpoint_id:path}/corrections/preview")
async def api_correction_preview(
    request: Request, endpoint_id: str, body: CorrectionRequest
) -> dict[str, Any]:
    from axon.v1.api_fix import build_plan, draft_from_prompt

    root = _repo_root(request, endpoint_id)
    snap = _snap(request)
    draft: CorrectionRequest | None = None
    draft_warnings: list[str] = []
    if body.prompt.strip() and not body.has_ops():
        from axon.config import get_settings

        llm = client_from_settings(get_settings())
        if llm is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "prompt drafting needs a configured LLM "
                    "(LLM_PROVIDER/LLM_BASE_URL/LLM_MODEL); "
                    "use the structured correction fields instead"
                ),
            )
        draft, draft_warnings = await draft_from_prompt(llm, snap, endpoint_id, body.prompt)
        if not draft.has_ops():
            return {
                "endpoint": endpoint_id,
                "draft": draft.model_dump(),
                "diffs": {},
                "effects": [],
                "warnings": draft_warnings or ["model drafted no correction"],
                "llm_configured": True,
            }
        body = draft
    try:
        _, _, preview = await asyncio.to_thread(build_plan, root, snap, endpoint_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **preview,
        "draft": draft.model_dump() if draft else None,
        "warnings": draft_warnings + preview["warnings"],
        "llm_configured": _llm_configured(),
    }


@router.post("/apis/{endpoint_id:path}/corrections/apply")
async def api_correction_apply(
    request: Request, endpoint_id: str, body: CorrectionRequest
) -> dict[str, Any]:
    import errno

    from axon.v1.api_fix import apply_plan

    if body.prompt.strip() and not body.has_ops():
        raise HTTPException(
            status_code=400,
            detail="preview the prompt draft and apply the structured correction",
        )
    root = _repo_root(request, endpoint_id)
    snap = _snap(request)
    try:
        result = await asyncio.to_thread(apply_plan, root, snap, endpoint_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        if isinstance(exc, PermissionError) or exc.errno == errno.EROFS:
            raise HTTPException(
                status_code=403,
                detail="The checkout is read-only. Apply the preview patch in a writable checkout.",
            ) from exc
        raise HTTPException(
            status_code=409, detail=f"Could not save the correction: {exc}"
        ) from exc
    job = await index_job_create(request, IndexRequest(repo_path=str(root), api=endpoint_id))
    job_id = json.loads(bytes(job.body)).get("job_id", "") if job.body else ""
    return {**result, "job_id": job_id}


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
    result = await ask_mod.ask(snap, body.question, llm=llm, store=store, engine=body.engine)
    # Model execution may outlive its drafting snapshot; only the result can
    # advertise its execution publication (empty means explicitly unknown).
    request.state.publication_id = result.publication_id
    return result


def _already_indexed(
    store: GraphStore,
    root: Path,
    commit: str,
    *,
    replace_source: bool = False,
    expected_repo: str = "",
) -> dict[str, Any] | None:
    """Result payload if this exact commit is already in the store."""
    if not commit:
        return None
    from axon.v1.revision import checkout_dirty
    from axon.v1.servicemap import service_map_fingerprint

    dirty = checkout_dirty(root)
    if dirty is not False:
        return None
    sm = load_service_map(root)
    if expected_repo and sm.repo != expected_repo:
        raise CheckoutChanged("repository ID changed before indexing; submit a new index request")
    snap = store.load()
    check_source_identity(snap, sm.repo, root, replace_source=replace_source)
    node = snap.nodes.get(repo_node_id(sm.repo))
    # A legacy pin/report cannot prove replay freshness, even with no calls.
    inputs = snap.link_inputs.get(sm.repo)
    report = snap.link_reports.get(sm.repo)
    if (
        inputs is None
        or not snap.complete_link_inputs(sm.repo)
        or report is None
        or not inputs.source_generation
        or any("link-inputs-missing" in line for line in report.diagnostics)
        or (node is not None and node.props.get("source_path", "") != str(root.resolve()))
    ):
        return None
    from axon.v1.fingerprints import INDEXER_FINGERPRINT

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
    from axon.v1.scoped_index import repository_view

    result = _index_result(repository_view(snap, sm.repo), sm.repo, [s.id for s in sm.services])
    resolve_index_commit(root, commit)
    return {**result, **publication_result(snap), "already_indexed": True, "commit": commit}


def _index_result(snap: GraphSnapshot, repo: str, services: list[str]) -> dict[str, Any]:
    return {
        **publication_result(snap),
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
    store = _store(request)
    try:
        repo_id = load_service_map(root).repo
        with JOBS.index_lock(str(root), repo_id):
            commit = resolve_index_commit(root, body.commit)
            if not body.force and not body.services and not body.api:
                hit = _already_indexed(
                    store,
                    root,
                    commit,
                    replace_source=body.replace_source,
                    expected_repo=repo_id,
                )
                if hit is not None:
                    return {**hit, "timings": dataclasses.asdict(timings)}
            snap, sm, indexing = index_and_publish(
                root,
                store,
                expected_repo=repo_id,
                commit=commit,
                services=body.services,
                api=body.api,
                base_commit=body.base_commit,
                force=body.force,
                replace_source=body.replace_source,
                timings=timings,
            )
    except (SourcePathConflict, CheckoutChanged) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = _index_result(snap, sm.repo, [s.id for s in sm.services])
    if body.api:
        result.update(
            {
                key: indexing[key]
                for key in (
                    "api",
                    "service",
                    "repo",
                    "changed",
                    "graph_delta",
                    "handlers",
                    "outbound",
                    "callers",
                )
                if key in indexing
            }
        )
    elif body.services:
        result.update(
            {key: indexing[key] for key in ("services", "coverage", "resolution", "graph_delta")}
        )
    return {
        "indexing": indexing,
        "warnings": indexing.get("warnings", []),
        "lost_cross_repo_links": indexing["lost_cross_repo_links"],
        "lost_cross_repo_link_count": indexing["lost_cross_repo_link_count"],
        **result,
        **{key: indexing[key] for key in publication_result(snap)},
        "timings": dataclasses.asdict(timings),
        "parse_warnings": timings.parse_warnings,
        "suppressed_calls": timings.suppressed_calls,
        "parse_warning_samples": snap.diagnostics[:10],
    }


@router.post("/index/jobs", status_code=202)
async def index_job_create(request: Request, body: IndexRequest) -> JSONResponse:
    root = Path(body.repo_path)
    from axon.v1.scoped_index import resolve_endpoint, scoped_paths

    def validate_submission() -> tuple[str, str]:
        if not root.is_dir():
            raise ValueError("repo_path is not a directory")
        sm = load_service_map(root)
        requested = resolve_index_commit(root, body.commit) if body.commit else ""
        snap = _snap(request)
        check_source_identity(snap, sm.repo, root, replace_source=body.replace_source)
        scoped_paths(root, sm, body.services)
        if body.api:
            resolve_endpoint(
                snap, body.api, body.services[0] if body.services else "", repo=sm.repo
            )
        return sm.repo, requested

    try:
        repo_id, requested = await asyncio.to_thread(validate_submission)
    except SourcePathConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store = _store(request)
    job = await asyncio.to_thread(JOBS.create, body.repo_path, repo_id=repo_id)
    cancel_event = await asyncio.to_thread(JOBS.cancel_event, job.id)
    last_phase = [""]
    last_done = [-1]

    def progress(phase: str, done: int, total: int) -> None:
        if phase != last_phase[0] or done - last_done[0] >= 25 or done == total:
            last_phase[0] = phase
            last_done[0] = done
            JOBS.update(job.id, phase=phase, done=done, total=total)

    def run() -> None:
        # A superseded or removed job may have been pruned before dispatch.
        if cancel_event is None:
            return
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
            commit = resolve_index_commit(root, requested)
            if not body.force and not body.services and not body.api:
                hit = _already_indexed(
                    store,
                    root,
                    commit,
                    replace_source=body.replace_source,
                    expected_repo=job.repo_id,
                )
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
                expected_repo=job.repo_id,
                services=body.services,
                api=body.api,
                base_commit=body.base_commit,
                force=body.force,
                replace_source=body.replace_source,
                commit=commit,
                timings=timings,
                progress=progress,
                cancelled=cancel_event,
            )
            result = _index_result(snap, sm.repo, [s.id for s in sm.services])
            if body.api:
                result.update(
                    {
                        key: indexing[key]
                        for key in (
                            "api",
                            "service",
                            "repo",
                            "changed",
                            "graph_delta",
                            "handlers",
                            "outbound",
                            "callers",
                        )
                        if key in indexing
                    }
                )
            elif body.services:
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
            result.update({key: indexing[key] for key in publication_result(snap)})
            result["indexing"] = indexing
            result["warnings"] = indexing.get("warnings", [])
            result["lost_cross_repo_links"] = indexing["lost_cross_repo_links"]
            result["lost_cross_repo_link_count"] = indexing["lost_cross_repo_link_count"]
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


@router.post("/impact/graph")
def graph_impact_endpoint(request: Request, body: GraphImpactRequest) -> dict[str, Any]:
    """Read the committed combined graph without indexing or publishing."""
    snap = _snap(request)
    repo = snap.nodes.get(repo_node_id(body.repo_id))
    if repo is None:
        raise HTTPException(status_code=404, detail=f"unknown repository: {body.repo_id}")
    indexed_commit = repo.props.get("commit", "")
    if body.expected_commit and indexed_commit != body.expected_commit:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "indexed-commit-mismatch",
                "indexed_commit": indexed_commit,
                "expected_commit": body.expected_commit,
            },
        )
    result = compute_impact(snap, body.changed_files, repo_id=body.repo_id)
    return {**result.model_dump(), "markdown": render_markdown(result)}


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


@router.get("/repos/{repo:path}/api-explain")
def maintenance_api_explain(
    request: Request, repo: str, api: str, service: str = ""
) -> dict[str, Any]:
    from axon.v1.diagnose import api_explain

    try:
        return api_explain(_snap(request), repo, api, service)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/repos/{repo:path}/clients")
def maintenance_clients(request: Request, repo: str) -> dict[str, Any]:
    from axon.v1.diagnose import client_list

    return {"clients": client_list(_snap(request), repo)}


@router.get("/repos/{repo:path}/clients/{client:path}")
def maintenance_client(request: Request, repo: str, client: str) -> dict[str, Any]:
    from axon.v1.diagnose import client_show

    try:
        return client_show(_snap(request), repo, client)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/repos/{repo:path}/services/{service:path}")
def maintenance_service(request: Request, repo: str, service: str) -> dict[str, Any]:
    from axon.v1.diagnose import service_show

    try:
        return service_show(_snap(request), repo, service)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/repos/{repo:path}/refs/{kind}/{entity:path}")
def maintenance_refs(request: Request, repo: str, kind: str, entity: str) -> dict[str, Any]:
    from axon.v1.diagnose import graph_refs

    try:
        return graph_refs(_snap(request), repo, kind, entity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
