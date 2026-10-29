"""Repo importer: files -> facts -> graph snapshot. No build tooling."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from axon.v2.facts import (
    CallFact,
    FileFacts,
    HttpFact,
    RouteFact,
    join_paths,
    normalize_path,
    path_signature,
)
from axon.v2.factscache import cache_dir, cache_key, load_facts, store_facts
from axon.v2.graph import GraphSnapshot
from axon.v2.linker import EndpointIndex, resolve_http, url_path
from axon.v2.models import (
    Confidence,
    EdgeKind,
    Evidence,
    GraphEdge,
    GraphNode,
    NodeKind,
    client_node_id,
    endpoint_node_id,
    external_node_id,
    file_node_id,
    repo_node_id,
    service_node_id,
    spec_node_id,
    symbol_node_id,
)
from axon.v2.parse_go import parse_go
from axon.v2.parse_java import parse_java
from axon.v2.parse_python import parse_python
from axon.v2.servicemap import ServiceMap, is_ignored, load_service_map, owner_of
from axon.workers.swagger.parser import parse_swagger_file

logger = logging.getLogger(__name__)

PARSERS = {".py": parse_python, ".java": parse_java, ".go": parse_go}
# Bump the version for a suffix whenever that parser's extraction
# behavior changes; cached facts under the old version are orphaned.
PARSER_VERSIONS = {".py": 4, ".java": 1, ".go": 1}
SPEC_NAMES = ("openapi", "swagger")


MAX_SNAPSHOT_DIAGNOSTICS = 100


class IndexCancelled(Exception):
    """Raised when the cancelled event is set between index stages."""


@dataclass
class IndexTimings:
    collect_s: float = 0.0
    parse_s: float = 0.0
    symbols_s: float = 0.0
    specs_s: float = 0.0
    routes_s: float = 0.0
    calls_s: float = 0.0
    clients_s: float = 0.0
    link_http_s: float = 0.0
    dedupe_s: float = 0.0
    total_s: float = 0.0
    files_total: int = 0
    files_parsed: int = 0
    files_cached: int = 0
    files_skipped: int = 0
    files_unowned: int = 0
    files_testlike: int = 0
    parse_warnings: int = 0  # reserved for parse-diagnostics work


@contextlib.contextmanager
def _timed(timings: IndexTimings, field_name: str) -> Iterator[None]:
    t = time.perf_counter()
    try:
        yield
    finally:
        setattr(timings, field_name, getattr(timings, field_name) + time.perf_counter() - t)


def _report(
    progress: Callable[[str, int, int], None] | None, phase: str, done: int, total: int
) -> None:
    if progress is None:
        return
    with contextlib.suppress(Exception):
        progress(phase, done, total)


def _check_cancelled(cancelled: threading.Event | None) -> None:
    if cancelled is not None and cancelled.is_set():
        raise IndexCancelled("index cancelled")


_TEST_DIRS = {
    "test",
    "tests",
    "testdata",
    "__tests__",
    "e2e",
    "integration-test",
    "integration-tests",
    "integrationtest",
    "test-utils",
    "testutils",
}


def _is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    dirs = [p.lower() for p in parts[:-1]]
    if any(p in _TEST_DIRS or p.startswith("test_") or p.endswith("_test") for p in dirs):
        return True
    if name == "conftest.py":
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith(
        ("_test.py", "_tests.py", "_test.go", "Test.java", "Tests.java", "TestCase.java", "IT.java")
    ):
        return True
    return any(
        seg == "src" and i + 1 < len(parts) and parts[i + 1] == "test"
        for i, seg in enumerate(parts)
    )


def _is_spec(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name.startswith(SPEC_NAMES) and name.rsplit(".", 1)[-1] in ("json", "yaml", "yml")


def collect_files(root: Path, sm: ServiceMap) -> list[str]:
    # Pruned walk: an ignored dir (node_modules, .worktrees, vendor, ...)
    # is never descended into, instead of walking it and filtering after.
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        kept = []
        for d in sorted(dirnames):
            rel_d = (rel_dir / d).as_posix()
            if not is_ignored(sm, f"{rel_d}/_"):
                kept.append(d)
        dirnames[:] = kept
        for f in sorted(filenames):
            rel = (rel_dir / f).as_posix()
            if is_ignored(sm, rel):
                continue
            if Path(f).suffix in PARSERS or _is_spec(rel):
                out.append(rel)
    return out


def _suffixes(qualified: str) -> list[str]:
    """Every dotted suffix: 'a.b.c' -> ['a.b.c', 'b.c', 'c']."""
    parts = qualified.split(".")
    return [".".join(parts[i:]) for i in range(len(parts))]


def _parse_one(
    task: tuple[str, str, str],
) -> tuple[str, str, FileFacts | None, str, bool]:
    """Worker: read + parse one file, through the facts cache.

    Returns (rel, text_sha256, facts, error, from_cache). cache_base ""
    disables the cache. Top-level so ProcessPoolExecutor can pickle it.
    """
    root_str, rel, cache_base = task
    try:
        raw = (Path(root_str) / rel).read_bytes()
    except OSError as exc:
        return rel, "", None, str(exc), False
    suffix = Path(rel).suffix
    key = ""
    if cache_base:
        key = cache_key(suffix, PARSER_VERSIONS[suffix], raw)
        hit = load_facts(key, Path(cache_base))
        if hit is not None:
            hit.path = rel  # only FileFacts carries the path; safe to rewrite
            text = raw.decode(encoding="utf-8", errors="replace")
            return rel, hashlib.sha256(text.encode()).hexdigest(), hit, "", True
    text = raw.decode(encoding="utf-8", errors="replace")
    try:
        facts = PARSERS[suffix](rel, text)
    except Exception as exc:  # parser bugs must not kill the import
        return rel, "", None, str(exc), False
    if key:
        store_facts(key, facts, Path(cache_base))
    return rel, hashlib.sha256(text.encode()).hexdigest(), facts, "", False


def _cache_enabled(use_cache: bool | None) -> bool:
    if use_cache is not None:
        return use_cache
    return os.environ.get("AXON_FACTS_CACHE", "").lower() not in ("0", "false", "off")


def _worker_count(workers: int | None) -> int:
    if workers is not None:
        return max(1, workers)
    env = os.environ.get("AXON_INDEX_WORKERS", "")
    if env.isdigit() and int(env) >= 1:
        return int(env)
    return os.cpu_count() or 1


def index_repo(
    repo_root: str | Path,
    snap: GraphSnapshot | None = None,
    commit: str = "",
    only_files: set[str] | None = None,
    workers: int | None = None,
    timings: IndexTimings | None = None,
    use_cache: bool | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    cancelled: threading.Event | None = None,
) -> tuple[GraphSnapshot, ServiceMap]:
    """Index one repo. ``workers`` (or AXON_INDEX_WORKERS, else CPU count)
    parallelizes per-file parsing; graph assembly stays sequential so the
    snapshot is identical either way. ``timings`` is filled when given;
    ``progress(phase, done, total)`` and ``cancelled`` support job runners."""
    total_t = time.perf_counter()
    timings = timings if timings is not None else IndexTimings()
    root = Path(repo_root)
    sm = load_service_map(root)
    repo_id = sm.repo
    snap = snap or GraphSnapshot(commit=commit)

    snap.add_node(
        GraphNode(
            id=repo_node_id(repo_id),
            kind=NodeKind.REPO,
            name=repo_id,
            repo_id=repo_id,
            props={
                "index_options": sm.index.fingerprint(),
                **({"commit": commit} if commit else {}),
            },
        )
    )
    for svc in sm.services:
        sid = service_node_id(repo_id, svc.id)
        snap.add_node(
            GraphNode(
                id=sid,
                kind=NodeKind.SERVICE,
                name=svc.name or svc.id,
                repo_id=repo_id,
                service_id=svc.id,
                props={
                    "language": svc.language,
                    "framework": svc.framework,
                    "roots": ",".join(svc.roots),
                },
            )
        )
        snap.add_edge(GraphEdge(from_id=repo_node_id(repo_id), to_id=sid, kind=EdgeKind.CONTAINS))

    with _timed(timings, "collect_s"):
        files = collect_files(root, sm)
    if only_files is not None:
        files = [f for f in files if f in only_files]

    all_facts: list[FileFacts] = []
    owners: dict[str, tuple[str, str]] = {}
    code_files = [f for f in files if Path(f).suffix in PARSERS]
    for rel in code_files:
        owners[rel] = owner_of(sm, rel)
    timings.files_unowned = sum(1 for rel in code_files if owners[rel][0] == "unowned")
    timings.files_testlike = sum(1 for rel in code_files if _is_test_path(rel))
    if code_files:
        logger.info(
            "index scope: %d code files (%d unowned, %d test-like)",
            len(code_files),
            timings.files_unowned,
            timings.files_testlike,
        )
    # Scope policy (axon.yaml `index:`): defaults preserve old behavior.
    kept: list[str] = []
    for rel in code_files:
        if not sm.index.include_unowned and owners[rel][0] == "unowned":
            timings.files_skipped += 1
            continue
        if sm.index.exclude_tests and _is_test_path(rel):
            timings.files_skipped += 1
            continue
        kept.append(rel)
    code_files = kept
    timings.files_total = len(code_files)
    _report(progress, "collect", len(code_files), len(code_files))
    cache_base = str(cache_dir()) if _cache_enabled(use_cache) else ""
    tasks = [(str(root), rel, cache_base) for rel in code_files]
    n_workers = _worker_count(workers)
    # Pool only pays off when parsing is the bottleneck; on small repos
    # process startup costs more than the parse itself.
    total_bytes = 0
    if n_workers > 1 and len(tasks) >= 64:
        for rel in code_files:
            try:
                total_bytes += (root / rel).stat().st_size
            except OSError:
                pass
    explicit = workers is not None or bool(os.environ.get("AXON_INDEX_WORKERS"))
    diagnostic_lines: list[str] = []
    total_parse_warnings = 0  # includes per-file truncated counts
    files_with_diagnostics = 0
    files_with_errors = 0
    with _timed(timings, "parse_s"):
        parsed: list[tuple[str, str, FileFacts | None, str, bool]] = []
        if n_workers > 1 and (explicit or (len(tasks) >= 64 and total_bytes >= 4_000_000)):
            with ProcessPoolExecutor(max_workers=n_workers) as pool:
                for i, item in enumerate(pool.map(_parse_one, tasks, chunksize=16), 1):
                    parsed.append(item)
                    if i % 50 == 0 or i == len(tasks):
                        _report(progress, "parse", i, len(tasks))
        else:
            for i, task in enumerate(tasks, 1):
                parsed.append(_parse_one(task))
                if i % 50 == 0 or i == len(tasks):
                    _report(progress, "parse", i, len(tasks))
        _check_cancelled(cancelled)
        for rel, sha256, facts, error, from_cache in parsed:
            kind_owner = owners[rel]
            if facts is None:
                timings.files_skipped += 1
                logger.warning("parse failed %s: %s", rel, error)
                continue
            if from_cache:
                timings.files_cached += 1
            else:
                timings.files_parsed += 1
            all_facts.append(facts)
            if facts.diagnostics or facts.diagnostics_truncated:
                files_with_diagnostics += 1
                total_parse_warnings += len(facts.diagnostics) + facts.diagnostics_truncated
                if any(d.category == "SyntaxError" for d in facts.diagnostics):
                    files_with_errors += 1
                for d in facts.diagnostics:
                    if d.category == "SyntaxError":
                        diagnostic_lines.append(f"{rel}:{d.line}: syntax error: {d.message}")
                    else:
                        diagnostic_lines.append(f"{rel}:{d.line}: {d.message}")
            _, service_id = kind_owner
            snap.add_node(
                GraphNode(
                    id=file_node_id(repo_id, rel),
                    kind=NodeKind.SOURCE_FILE,
                    name=rel,
                    repo_id=repo_id,
                    service_id=service_id,
                    file=rel,
                    props={
                        "language": facts.language,
                        "sha256": sha256,
                        "owner_kind": kind_owner[0],
                    },
                )
            )
            if service_id:
                snap.add_edge(
                    GraphEdge(
                        from_id=service_node_id(repo_id, service_id),
                        to_id=file_node_id(repo_id, rel),
                        kind=EdgeKind.CONTAINS,
                    )
                )

    _check_cancelled(cancelled)
    _report(progress, "symbols", 0, 1)
    with _timed(timings, "symbols_s"):
        _add_symbols(snap, repo_id, all_facts, owners)
    _check_cancelled(cancelled)
    _report(progress, "specs", 0, 1)
    with _timed(timings, "specs_s"):
        _add_specs(snap, root, repo_id, sm, files)
    _report(progress, "routes", 0, 1)
    with _timed(timings, "routes_s"):
        _add_routes(snap, repo_id, all_facts, owners)
    _check_cancelled(cancelled)
    _synthesize_call_throughs(all_facts, sm, cancelled)
    _report(progress, "calls", 0, 1)
    with _timed(timings, "calls_s"):
        _add_calls(snap, repo_id, all_facts, owners)
    _check_cancelled(cancelled)
    _report(progress, "clients", 0, 1)
    with _timed(timings, "clients_s"):
        _add_clients(snap, repo_id, sm)
    _report(progress, "link_http", 0, 1)
    with _timed(timings, "link_http_s"):
        _link_http(snap, repo_id, sm, all_facts)
    _report(progress, "dedupe", 0, 1)
    with _timed(timings, "dedupe_s"):
        snap.dedupe_edges()
    snap.diagnostics = diagnostic_lines[:MAX_SNAPSHOT_DIAGNOSTICS]
    overflow = len(diagnostic_lines) - len(snap.diagnostics)
    if overflow > 0:
        snap.diagnostics.append(f"... and {overflow} more parse warnings")
    timings.parse_warnings = total_parse_warnings
    if total_parse_warnings:
        logger.info(
            "suppressed %d invalid-escape warning(s) in %d file(s) during parse",
            total_parse_warnings,
            files_with_diagnostics,
        )
    if files_with_errors:
        logger.warning(
            "%d file(s) could not be parsed (syntax error); see diagnostics",
            files_with_errors,
        )
    timings.total_s = time.perf_counter() - total_t
    _report(progress, "done", 1, 1)
    return snap, sm


def _sym_id(repo_id: str, path: str, qualified: str) -> str:
    return symbol_node_id(repo_id, path, qualified)


def _add_symbols(
    snap: GraphSnapshot,
    repo_id: str,
    all_facts: list[FileFacts],
    owners: dict[str, tuple[str, str]],
) -> None:
    for facts in all_facts:
        _, service_id = owners.get(facts.path, ("unowned", ""))
        for sym in facts.symbols:
            nid = _sym_id(repo_id, facts.path, sym.qualified)
            snap.add_node(
                GraphNode(
                    id=nid,
                    kind=NodeKind.SYMBOL,
                    name=sym.name,
                    repo_id=repo_id,
                    service_id=service_id,
                    file=facts.path,
                    line_start=sym.line_start,
                    line_end=sym.line_end,
                    props={
                        "qualified": sym.qualified,
                        "kind": sym.kind,
                        "signature": sym.signature,
                        "language": facts.language,
                    },
                )
            )
            snap.add_edge(
                GraphEdge(
                    from_id=file_node_id(repo_id, facts.path),
                    to_id=nid,
                    kind=EdgeKind.DEFINES,
                    evidence=Evidence(file=facts.path, line=sym.line_start),
                )
            )


def _endpoint_node(
    snap: GraphSnapshot,
    repo_id: str,
    service_id: str,
    method: str,
    path: str,
    **props: str,
) -> str:
    eid = endpoint_node_id(repo_id, service_id, method, normalize_path(path))
    merged = {"method": method.upper(), "path": normalize_path(path)}
    merged.update({k: str(v) for k, v in props.items() if v})
    snap.add_node(
        GraphNode(
            id=eid,
            kind=NodeKind.API_ENDPOINT,
            name=f"{method.upper()} {normalize_path(path)}",
            repo_id=repo_id,
            service_id=service_id,
            props=merged,
        )
    )
    snap.add_edge(
        GraphEdge(from_id=service_node_id(repo_id, service_id), to_id=eid, kind=EdgeKind.EXPOSES)
    )
    return eid


def _add_specs(
    snap: GraphSnapshot,
    root: Path,
    repo_id: str,
    sm: ServiceMap,
    files: list[str],
) -> None:
    spec_files: dict[str, str] = {}  # path -> service_id
    for svc in sm.services:
        for sp in svc.specs:
            spec_files[sp] = svc.id
    for rel in files:
        if _is_spec(rel) and rel not in spec_files:
            _, service_id = owner_of(sm, rel)
            if service_id:
                spec_files[rel] = service_id
    for rel, service_id in sorted(spec_files.items()):
        parsed = parse_swagger_file(rel, str(root))
        if parsed is None:
            snap.unresolved.append(f"spec unparseable: {rel}")
            continue
        sid = spec_node_id(repo_id, rel)
        snap.add_node(
            GraphNode(
                id=sid,
                kind=NodeKind.API_SPEC,
                name=rel,
                repo_id=repo_id,
                service_id=service_id,
                file=rel,
                props={
                    "openapi_version": parsed.openapi_version,
                    "content_hash": parsed.content_hash,
                    "title": parsed.title,
                },
            )
        )
        for ep in parsed.endpoints:
            eid = _endpoint_node(
                snap,
                repo_id,
                service_id,
                ep.method,
                ep.path,
                operation_id=ep.operation_id or "",
                source="spec",
                summary=ep.summary or "",
                deprecated="true" if ep.deprecated else "",
            )
            snap.nodes[eid].file = snap.nodes[eid].file or rel
            snap.add_edge(
                GraphEdge(
                    from_id=sid,
                    to_id=eid,
                    kind=EdgeKind.DEFINES,
                    evidence=Evidence(file=rel, rule="spec:openapi"),
                )
            )


def _symbol_index(snap: GraphSnapshot, repo_id: str) -> dict[str, list[GraphNode]]:
    """Handler/call lookup by every dotted suffix of qualified name + leaf."""
    index: dict[str, list[GraphNode]] = {}
    for n in snap.by_kind(NodeKind.SYMBOL):
        if n.repo_id != repo_id:
            continue
        keys = set(_suffixes(n.props.get("qualified", ""))) | {n.name}
        for key in keys:
            if key:
                index.setdefault(key, []).append(n)
    return index


def _find_symbol(index: dict[str, list[GraphNode]], handler: str, service_id: str) -> str | None:
    cands = index.get(handler, [])
    if not cands:
        return None
    return sorted(cands, key=lambda n: (n.service_id != service_id, n.id))[0].id


_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def _effective_routes(all_facts: list[FileFacts]) -> dict[str, list[RouteFact]]:
    """Routes per file with Django include() mounts expanded.

    A module mounted via include("app.urls") contributes its routes
    under the mount prefix at the including file, and does not also
    contribute them unprefixed.
    """
    by_path = {f.path: f for f in all_facts}
    module_map: dict[str, str] = {}
    for f in all_facts:
        if f.path.endswith(".py"):
            for suffix in _suffixes(f.path[:-3].replace("/", ".")):
                module_map.setdefault(suffix, f.path)
    included = {
        module_map[i.module] for f in all_facts for i in f.includes if i.module in module_map
    }
    expanded: set[str] = set()

    def expand(path: str, prefix: str, depth: int, seen: set[str]) -> list[RouteFact]:
        expanded.add(path)
        facts = by_path[path]
        out = [replace(r, path=join_paths(prefix, r.path)) if prefix else r for r in facts.routes]
        if depth >= 8:
            return out
        for inc in facts.includes:
            target = module_map.get(inc.module)
            if target and target not in seen:
                out.extend(
                    expand(target, join_paths(prefix, inc.prefix), depth + 1, seen | {target})
                )
        return out

    result: dict[str, list[RouteFact]] = {}
    for f in all_facts:
        if f.path not in included:
            result[f.path] = expand(f.path, "", 0, {f.path})
    for f in all_facts:  # include cycles: fall back to the file's own routes
        if f.path not in expanded:
            result[f.path] = list(f.routes)
    return result


def _add_routes(
    snap: GraphSnapshot,
    repo_id: str,
    all_facts: list[FileFacts],
    owners: dict[str, tuple[str, str]],
) -> None:
    sym_index = _symbol_index(snap, repo_id)
    effective = _effective_routes(all_facts)
    # Class-based views: verb methods keyed by owning class qualified name.
    class_methods: dict[str, list[GraphNode]] = {}
    for n in snap.by_kind(NodeKind.SYMBOL):
        qualified = n.props.get("qualified", "")
        if n.props.get("kind") == "METHOD" and n.name.lower() in _HTTP_VERBS and "." in qualified:
            class_methods.setdefault(qualified.rsplit(".", 1)[0], []).append(n)
    # Spec endpoints an ANY-method (Django) route can attach to.
    spec_by_sig: dict[tuple[str, str], list[GraphNode]] = {}
    for ep in snap.by_kind(NodeKind.API_ENDPOINT):
        if ep.repo_id == repo_id and ep.props.get("source") in ("spec", "both"):
            key = (ep.service_id, path_signature(ep.props.get("path", "")))
            spec_by_sig.setdefault(key, []).append(ep)
    for facts in all_facts:
        _, service_id = owners.get(facts.path, ("unowned", ""))
        for route in effective.get(facts.path, []):
            if not service_id:
                snap.unresolved.append(
                    f"route outside any service root: {facts.path}:{route.line} "
                    f"{route.method} {route.path}"
                )
                continue
            eids: list[str] = []
            if route.method == "ANY":
                spec_eps = [
                    ep
                    for ep in spec_by_sig.get((service_id, path_signature(route.path)), [])
                    if ep.props.get("method") not in ("ANY", "")
                ]
                if spec_eps:
                    for ep in spec_eps:
                        ep.props["source"] = "both"
                        ep.props.setdefault("framework", route.framework)
                        ep.file = ep.file or facts.path
                        eids.append(ep.id)
            if not eids:
                eid = _endpoint_node(
                    snap,
                    repo_id,
                    service_id,
                    route.method,
                    route.path,
                    operation_id=route.operation_id,
                    source="code",
                    framework=route.framework,
                )
                node = snap.nodes[eid]
                if node.props.get("source") == "spec":
                    node.props["source"] = "both"
                node.file = node.file or facts.path
                node.line_start = node.line_start or route.line
                eids = [eid]
            handler_id = _sym_id(repo_id, facts.path, route.handler)
            if handler_id not in snap.nodes:
                found = _find_symbol(sym_index, route.handler, service_id)
                handler_id = found or handler_id
            if handler_id not in snap.nodes:
                # Stub so HANDLES still exists (e.g. Django view elsewhere).
                stub = GraphNode(
                    id=handler_id,
                    kind=NodeKind.SYMBOL,
                    name=route.handler.rsplit(".", 1)[-1],
                    repo_id=repo_id,
                    service_id=service_id,
                    file=facts.path,
                    line_start=route.line,
                    props={"qualified": route.handler, "kind": "FUNCTION", "stub": "true"},
                )
                snap.add_node(stub)
                for skey in {*_suffixes(route.handler), stub.name}:
                    if skey:
                        sym_index.setdefault(skey, []).append(stub)
            handler_ids = [handler_id]
            handler_node = snap.nodes.get(handler_id)
            if handler_node is not None and handler_node.props.get("kind") == "CLASS":
                methods = class_methods.get(handler_node.props.get("qualified", ""), [])
                if route.method != "ANY":
                    verb = [m for m in methods if m.name.lower() == route.method.lower()]
                    methods = verb or methods
                if methods:
                    # HANDLES from each verb method (View.put, View.get),
                    # so the call closure starts where calls are made.
                    handler_ids = [m.id for m in methods]
            for hid in handler_ids:
                for eid in eids:
                    snap.add_edge(
                        GraphEdge(
                            from_id=hid,
                            to_id=eid,
                            kind=EdgeKind.HANDLES,
                            evidence=Evidence(
                                file=facts.path, line=route.line, rule=f"route:{route.framework}"
                            ),
                        )
                    )


def _synthesize_call_throughs(
    all_facts: list[FileFacts],
    sm: ServiceMap,
    cancelled: threading.Event | None = None,
) -> None:
    """Resolve callable pass-through and factory-bound client receivers.

    Mutates all_facts by appending synthetic CallFacts/HttpFacts so the
    normal _add_calls / _link_http stages see through patterns like:
    view -> call_payments_api(payments_api.v1_list) -> call_with_retry -> fn().
    Only references that resolve (symbol, declared client factory, or
    operation name) produce facts; anything else stays unresolved.
    """
    facts_by_path = {f.path: f for f in all_facts}
    syms: dict[tuple[str, str], object] = {}
    for f in all_facts:
        for s in f.symbols:
            syms[(f.path, s.qualified)] = s
    params_of = {k: list(getattr(s, "params", [])) for k, s in syms.items()}
    calls_of: dict[tuple[str, str], list[CallFact]] = {}
    for f in all_facts:
        for c in f.calls:
            if c.caller:
                calls_of.setdefault((f.path, c.caller), []).append(c)
    http_syms = {(f.path, h.caller) for f in all_facts for h in f.http_calls}

    # Lookup indexes, built once. by_suffix[key] holds every symbol whose
    # qualified name equals key or ends with "." + key (one entry per
    # dotted suffix, via _suffixes); by_leaf is the fallback. This keeps
    # resolve() O(1) per call instead of a full scan of all symbols,
    # which made large repos hang in this stage.
    by_suffix: dict[str, list[tuple[str, str]]] = {}
    by_leaf: dict[str, list[tuple[str, str]]] = {}
    for key in syms:
        for suffix in _suffixes(key[1]):
            by_suffix.setdefault(suffix, []).append(key)
        by_leaf.setdefault(key[1].rsplit(".", 1)[-1], []).append(key)
    resolve_cache: dict[tuple[str, str], list[tuple[str, str]]] = {}

    def resolve(ref: str, from_path: str) -> list[tuple[str, str]]:
        cache_key = (ref, from_path)
        cached = resolve_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        hits = list(by_suffix.get(ref, ()))
        if not hits:
            hits = list(by_leaf.get(ref.rsplit(".", 1)[-1], ()))
        hits.sort(key=lambda k: (k[0] != from_path, k[0], k[1]))
        resolve_cache[cache_key] = hits
        return list(hits)

    factories: list[tuple[str, Any]] = [
        (fact, client) for svc in sm.services for client in svc.clients for fact in client.factories
    ]

    def client_for_factory(source: str) -> Any | None:
        for fact, client in factories:
            if source == fact or source.endswith("." + fact):
                return client
        # get_V1Api(paymentscli, ...) binds the receiver to that
        # package directly; a declared client owning the package matches.
        for svc in sm.services:
            for client in svc.clients:
                if any(source == p or source.startswith(p + ".") for p in client.packages):
                    return client
        return None

    def bindings_at(path: str, caller: str) -> dict[str, str]:
        f = facts_by_path.get(path)
        if f is None:
            return {}
        return {b.var: b.source for b in f.bindings if b.caller == caller}

    # Which function parameters actually get invoked (directly, or by
    # forwarding to another transparent wrapper's invoked parameter).
    transparent: dict[tuple[str, str], set[str]] = {k: set() for k in syms}
    for _round in range(10):
        _check_cancelled(cancelled)
        changed = False
        for n_key, (key, plist) in enumerate(params_of.items()):
            if n_key % 4096 == 0:
                _check_cancelled(cancelled)
            for p in plist:
                if p in transparent[key]:
                    continue
                for c in calls_of.get(key, []):
                    invoked = c.called == p
                    if not invoked:
                        for idx, ref in c.callable_args:
                            if ref != p:
                                continue
                            for callee in resolve(c.called, key[0]):
                                cp = params_of.get(callee, [])
                                if idx < len(cp) and cp[idx] in transparent.get(callee, set()):
                                    invoked = True
                    if invoked:
                        transparent[key].add(p)
                        changed = True
                        break
        if not changed:
            break

    emitted: set[tuple[str, str, int, str]] = set()

    def emit(facts: FileFacts, caller: str, ref: str, line: int) -> None:
        tag = (facts.path, caller, line, ref)
        if tag in emitted:
            return
        emitted.add(tag)
        hits = resolve(ref, facts.path)
        method = ref.rsplit(".", 1)[-1]
        receiver = ref.rsplit(".", 1)[0] if "." in ref else ""
        client = None
        if receiver:
            source = bindings_at(facts.path, caller).get(receiver.split(".")[0], "")
            client = client_for_factory(source) if source else None
        if hits and any(h in http_syms for h in hits):
            facts.calls.append(CallFact(caller, ref, line))
        elif client is not None and client.packages:
            facts.http_calls.append(
                HttpFact(caller, line, "", "", "call-through", f"{client.packages[0]}.{method}")
            )
        elif hits:
            facts.calls.append(CallFact(caller, ref, line))
        # Unresolvable dotted refs emit nothing: never guess a client.

    for facts in all_facts:
        _check_cancelled(cancelled)
        for c in list(facts.calls):
            if not c.caller:
                continue
            caller_key = (facts.path, c.caller)
            # Direct invocation on a factory-bound receiver.
            if "." in c.called:
                receiver = c.called.rsplit(".", 1)[0].split(".")[0]
                source = bindings_at(facts.path, c.caller).get(receiver, "")
                if source and client_for_factory(source) is not None:
                    emit(facts, c.caller, c.called, c.line)
            # Passing a callable into a transparent wrapper.
            for callee in resolve(c.called, facts.path):
                callee_params = params_of.get(callee, [])
                for idx, ref in c.callable_args:
                    if idx >= len(callee_params):
                        continue
                    if callee_params[idx] not in transparent.get(callee, set()):
                        continue
                    if ref in params_of.get(caller_key, []):
                        continue  # forwarding our own param; handled upstream
                    emit(facts, c.caller, ref, c.line)


def _add_calls(
    snap: GraphSnapshot,
    repo_id: str,
    all_facts: list[FileFacts],
    owners: dict[str, tuple[str, str]],
) -> None:
    by_suffix: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    for n in snap.by_kind(NodeKind.SYMBOL):
        if n.repo_id != repo_id:
            continue
        for key in _suffixes(n.props.get("qualified", "")):
            if key:
                by_suffix.setdefault(key, []).append(n.id)
        by_name.setdefault(n.name, []).append(n.id)

    for facts in all_facts:
        for call in facts.calls:
            if not call.caller:
                continue
            caller_id = _sym_id(repo_id, facts.path, call.caller)
            if caller_id not in snap.nodes:
                continue
            called = call.called
            if called.startswith("self."):
                # self.method -> Class.method within caller's class
                cls = call.caller.rsplit(".", 1)[0] if "." in call.caller else ""
                called = f"{cls}.{called[5:]}" if cls else called[5:]
            targets = list(by_suffix.get(called, []))
            if not targets:
                targets = by_name.get(called.rsplit(".", 1)[-1], [])
            targets = [t for t in dict.fromkeys(targets) if t != caller_id]
            if not targets:
                continue
            # Client-looking receivers (payment_client.create_charge) should
            # resolve to the client symbol, not the like-named API handler.
            owner_part = called.rsplit(".", 1)[0] if "." in called else ""
            if "client" in owner_part.lower() and len(targets) > 1:
                preferred = [
                    t
                    for t in targets
                    if owners.get(snap.nodes[t].file, ("", ""))[0] == "client"
                    or "client" in snap.nodes[t].props.get("qualified", "").lower()
                ]
                if len(preferred) == 1:
                    targets = preferred
            conf = Confidence.EXTRACTED if len(targets) == 1 else Confidence.AMBIGUOUS
            for t in targets[:5]:
                snap.add_edge(
                    GraphEdge(
                        from_id=caller_id,
                        to_id=t,
                        kind=EdgeKind.CALLS,
                        confidence=conf,
                        evidence=Evidence(
                            file=facts.path, line=call.line, rule="call:name-resolution"
                        ),
                    )
                )


def _add_clients(snap: GraphSnapshot, repo_id: str, sm: ServiceMap) -> None:
    for svc in sm.services:
        for client in svc.clients:
            cid = client_node_id(repo_id, client.id)
            snap.add_node(
                GraphNode(
                    id=cid,
                    kind=NodeKind.API_CLIENT,
                    name=client.id,
                    repo_id=repo_id,
                    service_id=svc.id,
                    props={
                        "target_service": client.target_service,
                        "target_repo": client.target_repo,
                        "packages": ",".join(client.packages),
                    },
                )
            )
            if client.spec:
                spec_repo, spec_path = repo_id, client.spec
                if "::" in client.spec:
                    spec_repo, spec_path = client.spec.split("::", 1)
                sid = spec_node_id(spec_repo, spec_path)
                if sid in snap.nodes:
                    snap.add_edge(
                        GraphEdge(
                            from_id=cid,
                            to_id=sid,
                            kind=EdgeKind.GENERATED_FROM,
                            confidence=Confidence.MANUAL,
                        )
                    )


def _link_http(
    snap: GraphSnapshot, repo_id: str, sm: ServiceMap, all_facts: list[FileFacts]
) -> None:
    index = EndpointIndex(snap)
    for facts in all_facts:
        for fact in facts.http_calls:
            caller_id = _sym_id(repo_id, facts.path, fact.caller)
            if caller_id not in snap.nodes:
                continue
            resolved = resolve_http(fact, index, sm, repo_id)
            if resolved is None:
                template = url_path(fact.url) or fact.call_name or "unknown"
                ext_id = external_node_id(fact.method or "ANY", template)
                snap.add_node(
                    GraphNode(
                        id=ext_id,
                        kind=NodeKind.EXTERNAL_API,
                        name=f"{fact.method or 'ANY'} {template}".strip(),
                        props={"method": fact.method, "url": fact.url, "call_name": fact.call_name},
                    )
                )
                snap.add_edge(
                    GraphEdge(
                        from_id=caller_id,
                        to_id=ext_id,
                        kind=EdgeKind.CALLS_API,
                        confidence=Confidence.AMBIGUOUS,
                        evidence=Evidence(
                            file=facts.path,
                            line=fact.line,
                            rule=f"unresolved:{fact.via}",
                            detail=fact.call_name,
                        ),
                        props={
                            "method": fact.method,
                            "url": fact.url,
                            "call_name": fact.call_name,
                            "via": fact.via,
                        },
                    )
                )
                snap.unresolved.append(
                    f"unresolved outbound call {facts.path}:{fact.line} "
                    f"{fact.call_name} {fact.url}".strip()
                )
            else:
                target_id, conf, rule = resolved
                if target_id not in snap.nodes:
                    snap.unresolved.append(
                        f"manual link target not indexed yet: {target_id} "
                        f"(from {facts.path}:{fact.line})"
                    )
                    continue
                snap.add_edge(
                    GraphEdge(
                        from_id=caller_id,
                        to_id=target_id,
                        kind=EdgeKind.CALLS_API,
                        confidence=conf,
                        evidence=Evidence(
                            file=facts.path, line=fact.line, rule=rule, detail=fact.call_name
                        ),
                        props={
                            "method": fact.method,
                            "url": fact.url,
                            "call_name": fact.call_name,
                            "via": fact.via,
                        },
                    )
                )
