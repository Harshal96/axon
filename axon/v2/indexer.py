"""Repo importer: files -> facts -> graph snapshot. No build tooling."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from axon.v2.client_discovery import discover_clients
from axon.v2.facts import (
    MAX_DIAGNOSTICS_PER_FILE,
    CallFact,
    FileFacts,
    HttpFact,
    ParseDiagnostic,
    RouteFact,
    SymFact,
    join_paths,
    normalize_path,
    path_signature,
    route_specificity,
)
from axon.v2.factscache import cache_dir, cache_key, load_facts, store_facts
from axon.v2.fingerprints import INDEXER_FINGERPRINT, PARSER_FINGERPRINTS
from axon.v2.graph import GraphSnapshot
from axon.v2.indexstate import FileState
from axon.v2.linker import (
    EndpointIndex,
    _operation_base,
    base_reference,
    operation_candidates,
    reconcile_path_fallbacks,
    resolve_http,
    url_path,
)
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
from axon.v2.revision import checkout_dirty
from axon.v2.sdk_catalog import sdk_provider, url_shaped
from axon.v2.servicemap import (
    ServiceMap,
    _matches,
    is_ignored,
    load_service_map,
    owner_match,
    owner_of,
    service_map_fingerprint,
)
from axon.workers.swagger.parser import parse_swagger_file

logger = logging.getLogger(__name__)

PARSERS = {".py": parse_python, ".java": parse_java, ".go": parse_go}
# Bump the version for a suffix whenever that parser's extraction
# behavior changes; cached facts under the old version are orphaned.
PARSER_VERSIONS = PARSER_FINGERPRINTS
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
    call_through_s: float = 0.0
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
    suppressed_calls: int = 0
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
    from axon.v2.onboard import _git_files

    git_files = _git_files(root)
    if git_files is not None:
        return sorted(
            {
                rel
                for rel in git_files
                if not is_ignored(sm, rel)
                and (root / rel).is_file()
                and (
                    Path(rel).suffix in PARSERS
                    or _is_spec(rel)
                    or Path(rel).name in {"swagger.cfg", "BUILD", "BUILD.bazel"}
                )
            }
        )
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
            if (
                Path(f).suffix in PARSERS
                or _is_spec(rel)
                or f in {"swagger.cfg", "BUILD", "BUILD.bazel"}
            ):
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
        if hit is not None and not any(d.severity == "file-degraded" for d in hit.diagnostics):
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
    reuse: dict[str, FileState] | None = None,
    dirty_paths: set[str] | None = None,
    capture: dict[str, Any] | None = None,
    previous: GraphSnapshot | None = None,
    force_parse_paths: set[str] | None = None,
) -> tuple[GraphSnapshot, ServiceMap]:
    """Index one repo. ``workers`` (or AXON_INDEX_WORKERS, else CPU count)
    parallelizes per-file parsing; graph assembly stays sequential so the
    snapshot is identical either way. ``timings`` is filled when given;
    ``progress(phase, done, total)`` and ``cancelled`` support job runners."""
    force_parse_paths = force_parse_paths or set()
    if dirty_paths is not None:
        dirty_paths = dirty_paths | force_parse_paths
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
                "axon_build": INDEXER_FINGERPRINT,
                "source_path": str(root.resolve()),
                "index_options": sm.index.fingerprint(),
                "service_map_hash": service_map_fingerprint(root),
                "dirty": {True: "true", False: "false", None: "unknown"}[checkout_dirty(root)],
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
        sm = discover_clients(root, sm, files)
    if only_files is not None:
        files = [f for f in files if f in only_files]

    if capture is not None:
        capture["files"] = list(files)
    all_facts: list[FileFacts] = []
    owners: dict[str, tuple[str, str]] = {}
    code_files = [f for f in files if Path(f).suffix in PARSERS]
    owner_roots: dict[str, str] = {}
    for rel in code_files:
        kind, service_id, pattern = owner_match(sm, rel)
        owners[rel] = (kind, service_id)
        owner_roots[rel] = pattern
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
    reused = {
        rel: reuse[rel]
        for rel in code_files
        if reuse and rel in reuse and dirty_paths is not None and rel not in dirty_paths
    }
    tasks = [
        (str(root), rel, "" if rel in force_parse_paths else cache_base)
        for rel in code_files
        if rel not in reused
    ]
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
        parsed: list[tuple[str, str, FileFacts | None, str, bool]] = [
            (rel, entry.content_hash, copy.deepcopy(entry.facts), "", True)
            for rel, entry in reused.items()
        ]
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
        for rel, sha256, facts, error, from_cache in sorted(parsed, key=lambda item: item[0]):
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
                        diagnostic_lines.append(
                            f"{rel}:{d.line}: "
                            + ("graph-unsound: " if d.severity == "graph-unsound" else "")
                            + d.message
                        )
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

    if capture is not None:
        capture["raw"] = {facts.path: copy.deepcopy(facts) for facts in all_facts}
    _check_cancelled(cancelled)
    _report(progress, "symbols", 0, 1)
    with _timed(timings, "symbols_s"):
        _add_symbols(snap, repo_id, all_facts, owners, owner_roots)
        for facts in all_facts:
            node = snap.nodes.get(file_node_id(repo_id, facts.path))
            if node is not None:
                node.props["imports"] = json.dumps(facts.imports)
    _check_cancelled(cancelled)
    _report(progress, "specs", 0, 1)
    with _timed(timings, "specs_s"):
        _add_specs(snap, root, repo_id, sm, files)
    _report(progress, "routes", 0, 1)
    with _timed(timings, "routes_s"):
        from axon.v2.java_constants import resolve_constants

        resolve_constants(all_facts)
        _add_routes(snap, repo_id, all_facts, owners)
        from axon.v2.manual_corrections import add_manual_routes, manual_sources

        sources = manual_sources(root)
        add_manual_routes(snap, repo_id, sm, sources)
    shared_routes = [
        f"{facts.path}:{route.line}"
        for facts in all_facts
        if owners.get(facts.path, ("", ""))[0] == "shared"
        for route in facts.routes
    ]
    if shared_routes:
        diagnostic_lines.append(
            f"routes found in shared code: {len(shared_routes)}; "
            "declare service roots for deployable applications; samples: "
            + ", ".join(shared_routes[:5])
        )
    _check_cancelled(cancelled)
    with _timed(timings, "call_through_s"):
        _synthesize_call_throughs(all_facts, sm, cancelled, dirty_paths)
        if reuse and dirty_paths is not None:
            restored: list[FileFacts] = []
            for facts in all_facts:
                saved = reuse.get(facts.path)
                if facts.path not in dirty_paths and saved and saved.resolved_facts is not None:
                    restored.append(copy.deepcopy(saved.resolved_facts))
                else:
                    restored.append(facts)
            all_facts = restored
        if capture is not None:
            capture["resolved"] = {f.path: copy.deepcopy(f) for f in all_facts}
    _report(progress, "calls", 0, 1)
    with _timed(timings, "calls_s"):
        if previous is not None and dirty_paths is not None:
            for edge in previous.edges:
                caller = previous.nodes.get(edge.from_id)
                if edge.kind == EdgeKind.CALLS and caller and caller.file not in dirty_paths:
                    if edge.from_id in snap.nodes and edge.to_id in snap.nodes:
                        snap.add_edge(copy.deepcopy(edge))
        _add_calls(snap, repo_id, all_facts, owners, dirty_paths)
    _check_cancelled(cancelled)
    _report(progress, "clients", 0, 1)
    with _timed(timings, "clients_s"):
        _add_clients(snap, repo_id, sm)
    _report(progress, "link_http", 0, 1)
    with _timed(timings, "link_http_s"):
        _link_http(snap, repo_id, sm, all_facts, sources)
        from axon.v2.manual_corrections import add_manual_calls, suppress_edges

        add_manual_calls(snap, repo_id, sm, sources)
        suppress_edges(snap, repo_id, sm)
    _report(progress, "dedupe", 0, 1)
    with _timed(timings, "dedupe_s"):
        snap.dedupe_edges()
    suppressed = [
        f"{f.path}:{d.line}: {d.message}"
        for f in all_facts
        for d in f.diagnostics
        if d.category == "Suppressed"
    ]
    diagnostic_lines.extend(suppressed)
    diagnostic_lines.extend(
        f"{f.path}:{d.line}: {d.message}"
        for f in all_facts
        for d in f.diagnostics
        if d.category == "untyped-client"
    )
    diagnostic_lines.extend(snap.diagnostics)
    timings.suppressed_calls = sum(f.suppressed_calls for f in all_facts)
    repo_marker = snap.nodes[repo_node_id(repo_id)]
    repo_marker.props["graph_unsound"] = str(
        any("graph-unsound:" in line for line in diagnostic_lines)
    ).lower()
    repo_marker.props["file_degraded"] = str(
        sum(any(d.severity == "file-degraded" for d in f.diagnostics) for f in all_facts)
    )
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
    for edge in snap.edges:
        source = snap.nodes.get(edge.from_id)
        edge.props.setdefault("owner_file", edge.evidence.file or (source.file if source else ""))
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
    owner_roots: dict[str, str] | None = None,
) -> None:
    for facts in all_facts:
        _, service_id = owners.get(facts.path, ("unowned", ""))
        groups: dict[str, list[SymFact]] = defaultdict(list)
        for sym in facts.symbols:
            groups[sym.qualified].append(sym)
        for declarations in groups.values():
            sym = declarations[-1]
            nid = _sym_id(repo_id, facts.path, sym.qualified)
            snap.add_node(
                GraphNode(
                    id=nid,
                    kind=NodeKind.SYMBOL,
                    name=sym.name,
                    repo_id=repo_id,
                    service_id=service_id,
                    file=facts.path,
                    line_start=min(item.line_start for item in declarations),
                    line_end=max(item.line_end for item in declarations),
                    props={
                        **({"overloads": str(len(declarations))} if len(declarations) > 1 else {}),
                        "qualified": sym.qualified,
                        "kind": sym.kind,
                        "signature": sym.signature,
                        "language": facts.language,
                        "owner_kind": owners.get(facts.path, ("unowned", ""))[0],
                        "owner_root": (owner_roots or {}).get(facts.path, ""),
                    },
                )
            )
            for declaration in declarations:
                snap.add_edge(
                    GraphEdge(
                        from_id=file_node_id(repo_id, facts.path),
                        to_id=nid,
                        kind=EdgeKind.DEFINES,
                        evidence=Evidence(file=facts.path, line=declaration.line_start),
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
    literals, catch_all = route_specificity(path)
    merged.update(literal_segments=str(literals), catch_all=str(catch_all).lower())
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
                base_path=parsed.base_path,
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
    """Expand module/router mounts while retaining each handler's source file."""
    modules: dict[str, list[str]] = {}
    resolver = _CallResolver(all_facts, {})
    for facts in all_facts:
        if facts.path.endswith(".py"):
            module = facts.path[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[:-9]
            for suffix in _suffixes(module):
                modules.setdefault(suffix, []).append(facts.path)

    def target(facts: FileFacts, ref: str, kind: str, symbol: str = "") -> tuple[str, str] | None:
        head, dot, tail = ref.partition(".")
        ref = facts.import_map.get(head, head) + (dot + tail if dot else "")
        if kind in ("fastapi", "drf"):
            if "." not in ref:
                return (facts.path, ref)
            module, router = ref.rsplit(".", 1)
        else:
            module, router = ref, symbol
            if symbol and not modules.get(module):
                module, router = ref + "." + symbol, ""
        candidates = modules.get(module, [])
        if not candidates:
            return None
        ranked = sorted(
            candidates,
            key=lambda p: (
                -len(
                    Path(
                        os.path.commonpath([str(Path(facts.path).parent), str(Path(p).parent)])
                    ).parts
                ),
                p,
            ),
        )
        if len(ranked) > 1:
            # A short module name cannot choose between equally near copies.
            def distance(p: str) -> int:
                return len(
                    Path(
                        os.path.commonpath([str(Path(facts.path).parent), str(Path(p).parent)])
                    ).parts
                )

            if distance(ranked[0]) == distance(ranked[1]):
                return None
        return ranked[0], router

    routes: dict[tuple[str, str], list[RouteFact]] = {}
    mounts: dict[tuple[str, str], list[tuple[tuple[str, str], str]]] = {}
    included: set[tuple[str, str]] = set()

    def actions(path: str, name: str, seen: set[tuple[str, str]]) -> dict[str, RouteFact]:
        if (path, name) in seen:
            return {}
        seen.add((path, name))
        facts = resolver.facts[path]
        found = {}
        for parent in facts.superclasses.get(name, []):
            for base_path, base_name in resolver.resolve(parent, path, name):
                found.update(actions(base_path, base_name, seen))
        framework_bases = [
            resolver.canonical(base, path) for base in facts.superclasses.get(name, [])
        ]
        if any(
            base
            in {
                "rest_framework.viewsets.ModelViewSet",
                "rest_framework.viewsets.ReadOnlyModelViewSet",
            }
            for base in framework_bases
        ):
            implicit = [("GET", "", "list"), ("GET", "/{pk}", "retrieve")]
            if "rest_framework.viewsets.ModelViewSet" in framework_bases:
                implicit += [
                    ("POST", "", "create"),
                    ("PUT", "/{pk}", "update"),
                    ("PATCH", "/{pk}", "partial_update"),
                    ("DELETE", "/{pk}", "destroy"),
                ]
            for verb, suffix, action_name in implicit:
                found.setdefault(
                    action_name, RouteFact(verb, suffix, name + "." + action_name, 1, "django")
                )
        for action in facts.view_actions.get(name, []):
            found[action.handler.rsplit(".", 1)[-1]] = action
        return found

    for facts in all_facts:
        for registration in facts.router_registrations:
            for path, name in resolver.resolve(registration.viewset, facts.path):
                for method_name, action in actions(path, name, set()).items():
                    routes.setdefault((facts.path, registration.router), []).append(
                        replace(
                            action,
                            path=join_paths(registration.prefix, action.path),
                            handler=registration.viewset + "." + method_name,
                            line=registration.line,
                        )
                    )
        for route in facts.routes:
            routes.setdefault((facts.path, route.router), []).append(route)
        for symbol, selected in facts.pattern_routes.items():
            routes.setdefault((facts.path, symbol), []).extend(selected)
        for inc in facts.includes:
            child = target(facts, inc.module, inc.kind, inc.symbol)
            if child is None:
                continue
            parent = (facts.path, inc.router if inc.kind == "fastapi" else "")
            mounts.setdefault(parent, []).append((child, inc.prefix))
            included.add(child)
    facts_by_path = {f.path: f for f in all_facts}
    result: dict[str, list[RouteFact]] = {f.path: [] for f in all_facts}
    expanded: set[tuple[str, str]] = set()

    def expand(key: tuple[str, str], prefix: str, seen: set[tuple[str, str]]) -> None:
        expanded.add(key)
        for route in routes.get(key, []):
            result[key[0]].append(
                replace(route, path=join_paths(prefix, route.path)) if prefix else route
            )
        if len(seen) >= 16:
            facts = facts_by_path.get(key[0])
            if facts is not None and mounts.get(key):
                facts.diagnostics.append(
                    ParseDiagnostic(
                        0,
                        "Resolution",
                        "route include targets truncated at depth 16",
                        severity="graph-unsound",
                    )
                )
            return
        for child, mount in mounts.get(key, []):
            if child not in seen:
                expand(child, join_paths(prefix, mount), seen | {child})

    keys = dict.fromkeys([*routes, *mounts])
    named_lists = {(f.path, name) for f in all_facts for name in f.pattern_routes}
    named_lists.update(
        (f.path, registration.router) for f in all_facts for registration in f.router_registrations
    )
    for key in keys:
        if key not in included and key not in named_lists:
            expand(key, "", {key})
    for key in keys:
        if key not in expanded and key not in named_lists:
            expand(key, "", {key})
    from axon.v2.framework_hooks import GENERIC_ACTIONS

    for path, selected in result.items():
        expanded_routes: list[RouteFact] = []
        for route in selected:
            verbs: set[str] = set()
            if route.method == "ANY":
                queue = list(resolver.resolve(route.handler, path))
                seen_types: set[tuple[str, str]] = set()
                while queue:
                    p, q = queue.pop()
                    if (p, q) in seen_types:
                        continue
                    seen_types.add((p, q))
                    verbs.update(
                        action.method
                        for action in resolver.facts[p].view_actions.get(q, [])
                        if not action.path
                    )
                    for base in resolver.facts[p].superclasses.get(q, []):
                        canonical = resolver.canonical(base, p)
                        if canonical.startswith("rest_framework."):
                            verb = GENERIC_ACTIONS.get(canonical.rsplit(".", 1)[-1], ("", ""))[0]
                            if verb:
                                verbs.add(verb)
                        queue.extend(resolver.resolve(base, p, q))
            expanded_routes.extend(
                replace(route, method=verb) for verb in sorted(verbs)
            ) if verbs else expanded_routes.append(route)
        result[path] = expanded_routes
    return result


def _add_routes(
    snap: GraphSnapshot,
    repo_id: str,
    all_facts: list[FileFacts],
    owners: dict[str, tuple[str, str]],
) -> None:
    sym_index = _symbol_index(snap, repo_id)
    resolver = _CallResolver(all_facts, owners)
    effective = _effective_routes(all_facts)
    snap.diagnostics.extend(
        f"graph-unsound: {f.path}:{d.line}: {d.message}"
        for f in all_facts
        for d in f.diagnostics
        if d.severity == "graph-unsound"
    )
    # Class-based views: verb methods keyed by owning class qualified name.
    class_methods: dict[tuple[str, str], list[GraphNode]] = {}
    for n in snap.by_kind(NodeKind.SYMBOL):
        qualified = n.props.get("qualified", "")
        if n.props.get("kind") == "METHOD" and n.name.lower() in _HTTP_VERBS and "." in qualified:
            class_methods.setdefault((n.file, qualified.rsplit(".", 1)[0]), []).append(n)
    # Spec endpoints an ANY-method (Django) route can attach to.
    spec_by_sig: dict[tuple[str, str], list[GraphNode]] = {}
    for ep in snap.by_kind(NodeKind.API_ENDPOINT):
        if ep.repo_id == repo_id and ep.props.get("source") in ("spec", "both"):
            path = ep.props.get("path", "")
            variants = {path}
            for prefix in {ep.props.get("base_path", ""), "/api"}:
                if prefix and path.startswith(prefix.rstrip("/") + "/"):
                    variants.add(path[len(prefix.rstrip("/")) :])
            for signature in {path_signature(variant) for variant in variants}:
                spec_by_sig.setdefault((ep.service_id, signature), []).append(ep)
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
            if route.method:
                spec_eps = [
                    ep
                    for ep in spec_by_sig.get((service_id, path_signature(route.path)), [])
                    if ep.props.get("method") not in ("ANY", "")
                    and (route.method == "ANY" or ep.props.get("method") == route.method)
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
                    catch_all=str(route.catch_all).lower() if route.catch_all else "",
                )
                node = snap.nodes[eid]
                if node.props.get("source") == "spec":
                    node.props["source"] = "both"
                node.file = node.file or facts.path
                node.line_start = node.line_start or route.line
                eids = [eid]
            handler_id = _sym_id(repo_id, facts.path, route.handler)
            if handler_id not in snap.nodes:
                candidates = resolver.resolve(route.handler, facts.path)
                if len(candidates) == 1:
                    handler_id = _sym_id(repo_id, *candidates[0])
            stub_path, stub_qualified = facts.path, route.handler
            stub_line = route.line
            inherited = False
            source_inherited = (
                handler_id in snap.nodes
                and snap.nodes[handler_id].props.get("qualified") != route.handler
                and route.framework == "django"
            )
            if handler_id not in snap.nodes and "." in route.handler:
                class_ref, member = route.handler.rsplit(".", 1)
                classes = resolver.resolve(class_ref, facts.path)
                if len(classes) == 1:
                    stub_path, cls = classes[0]
                    stub_qualified = cls + "." + member
                    stub_line = resolver.symbols[(stub_path, cls)].line_start
                    inherited = route.framework == "django"
                    handler_id = _sym_id(repo_id, stub_path, cls if inherited else stub_qualified)
            if handler_id not in snap.nodes:
                # Stub so HANDLES still exists (e.g. Django view elsewhere).
                stub = GraphNode(
                    id=handler_id,
                    kind=NodeKind.SYMBOL,
                    name=route.handler.rsplit(".", 1)[-1],
                    repo_id=repo_id,
                    service_id=service_id,
                    file=stub_path,
                    line_start=stub_line,
                    props={
                        "qualified": stub_qualified,
                        "kind": "FUNCTION",
                        "stub": "true",
                        **(
                            {
                                "rule": "framework:drf-inherited",
                                "route_file": facts.path,
                                "route_line": str(route.line),
                            }
                            if inherited
                            else {}
                        ),
                    },
                )
                snap.add_node(stub)
                for skey in {*_suffixes(route.handler), stub.name}:
                    if skey:
                        sym_index.setdefault(skey, []).append(stub)
            handler_ids = [handler_id]
            handler_node = snap.nodes.get(handler_id)
            if (
                not inherited
                and handler_node is not None
                and handler_node.props.get("kind") == "CLASS"
            ):
                methods = class_methods.get(
                    (handler_node.file, handler_node.props.get("qualified", "")), []
                )
                if route.method != "ANY":
                    verb = [m for m in methods if m.name.lower() == route.method.lower()]
                    methods = verb or methods
                if methods:
                    # HANDLES from each verb method (View.put, View.get),
                    # so the call closure starts where calls are made.
                    handler_ids = [m.id for m in methods]
            for hid in handler_ids:
                for eid in eids:
                    if handler_node is not None and handler_node.props.get("kind") == "CLASS":
                        method = snap.nodes[eid].props.get("method", "").lower()
                        if (
                            hid != handler_id
                            and method in _HTTP_VERBS
                            and snap.nodes[hid].name.lower() != method
                        ):
                            continue
                    snap.add_edge(
                        GraphEdge(
                            from_id=hid,
                            to_id=eid,
                            kind=EdgeKind.HANDLES,
                            props={
                                "implicit_action": member,
                                "inherited_from": ", ".join(
                                    resolver.facts[stub_path].superclasses.get(cls, [])
                                ),
                            }
                            if inherited
                            else {},
                            evidence=Evidence(
                                file=snap.nodes[hid].file
                                if snap.nodes[hid].props.get("rule") == "framework:drf-inherited"
                                else facts.path,
                                line=snap.nodes[hid].line_start
                                if snap.nodes[hid].props.get("rule") == "framework:drf-inherited"
                                else route.line,
                                rule="framework:drf-implicit(inherited)"
                                if source_inherited
                                else "framework:drf-implicit"
                                if inherited
                                else snap.nodes[hid].props.get("rule", f"route:{route.framework}"),
                                detail=f"route {facts.path}:{route.line}"
                                if snap.nodes[hid].props.get("rule") == "framework:drf-inherited"
                                else "",
                            ),
                        )
                    )


def _synthesize_call_throughs(
    all_facts: list[FileFacts],
    sm: ServiceMap,
    cancelled: threading.Event | None = None,
    dirty_paths: set[str] | None = None,
) -> None:
    """Resolve callable pass-through and factory-bound client receivers.

    Mutates all_facts by appending synthetic CallFacts/HttpFacts so the
    normal _add_calls / _link_http stages see through patterns like:
    view -> call_payments_api(payments_api.v1_list) -> call_with_retry -> fn().
    Only references that resolve (symbol, declared client factory, or
    operation name) produce facts; anything else stays unresolved.
    """
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

    resolver = _CallResolver(all_facts, {f.path: owner_of(sm, f.path) for f in all_facts})

    def resolve(ref: str, from_path: str, caller: str = "") -> list[tuple[str, str]]:
        return resolver.resolve(ref, from_path, caller)

    for facts in all_facts:
        if dirty_paths is not None and facts.path not in dirty_paths:
            continue
        additions: list[HttpFact] = []
        for http in facts.http_calls:
            variants = [http.url]
            for constant in re.findall(r"\{setting:([^}]+)\}", http.url):
                module, _, name = constant.rpartition(".")
                constant_values = {
                    value
                    for p in resolver._owned_paths(resolver.modules.get(module, []), facts.path)
                    for value in resolver.facts[p].constants.get(name, [])
                }
                if constant_values:
                    variants = [
                        url.replace("{setting:" + constant + "}", value)
                        for url in variants
                        for value in sorted(constant_values)
                    ][:25]
            refs = list(
                dict.fromkeys(
                    ref for url in variants for ref in re.findall(r"\{attr:([^}]+)\}", url)
                )
            )
            for ref in refs:
                values = [
                    value
                    for value in resolver.receiver_types(ref, facts.path, http.caller)
                    if value.startswith(("http://", "https://", "{env:", "{setting:"))
                ]
                if values:
                    variants = [
                        url.replace("{attr:" + ref + "}", value)
                        for url in variants
                        for value in values
                    ][:25]
            if variants:
                http.url = variants[0]
                additions.extend(replace(http, url=url) for url in variants[1:])
        facts.http_calls.extend(additions)

    # A wrapper that forwards a parameter to a known HTTP transport provides
    # stronger evidence than its unresolved transport call alone. Instantiate
    # its URL/method at callers that supply static strings.
    wrappers = {
        (f.path, h.caller): [
            item for item in f.http_calls if item.caller == h.caller and item.url_param
        ]
        for f in all_facts
        for h in f.http_calls
        if h.url_param
    }
    wrapper_names = {name.rsplit(".", 1)[-1] for _, name in wrappers}
    for facts in all_facts:
        if dirty_paths is not None and facts.path not in dirty_paths:
            continue
        for call in facts.calls:
            if not call.caller or call.called.rsplit(".", 1)[-1] not in wrapper_names:
                continue
            for target in resolve(call.called, facts.path, call.caller):
                for transport in wrappers.get(target, []):
                    params = params_of.get(target, [])

                    def argument(name: str) -> str:
                        if name in call.arguments:
                            return call.arguments[name]
                        return (
                            call.arguments.get(str(params.index(name)), "")
                            if name in params
                            else ""
                        )

                    url = argument(transport.url_param)
                    method = (
                        argument(transport.method_param)
                        if transport.method_param
                        else transport.method
                    )
                    if url.startswith(("http://", "https://", "/", "{env:", "{setting:", "{attr:")):
                        facts.http_calls.append(
                            HttpFact(
                                call.caller,
                                call.line,
                                method.upper(),
                                url,
                                "http-wrapper",
                                call.called,
                            )
                        )

    factories: list[tuple[str, Any]] = [
        (fact, client) for svc in sm.services for client in svc.clients for fact in client.factories
    ]

    client_owners = {id(client): svc.id for svc in sm.services for client in svc.clients}

    source_clients_cache: dict[str, list[Any]] = {}

    def clients_for_source(source: str) -> list[Any]:
        if source in source_clients_cache:
            return source_clients_cache[source]
        matches = {
            (client_owners[id(client)], client.id): client
            for factory, client in factories
            if source == factory or source.endswith("." + factory)
        }
        for svc in sm.services:
            for client in svc.clients:
                if any(
                    source == package or source.startswith(package + ".")
                    for package in client.packages
                ):
                    matches[(svc.id, client.id)] = client
        result = list(matches.values())
        source_clients_cache[source] = result
        return result

    def client_for_factory(source: str) -> Any | None:
        matches = clients_for_source(source)
        return matches[0] if matches else None

    def bindings_at(path: str, caller: str) -> dict[str, str]:
        return {
            **resolver.bindings.get((path, ""), {}),
            **resolver.bindings.get((path, caller), {}),
        }

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
                            for callee in resolve(c.called, key[0], key[1]):
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

    def suppress(facts: FileFacts, ref: str, line: int) -> None:
        facts.suppressed_calls += 1
        if sum(d.category == "Suppressed" for d in facts.diagnostics) < MAX_DIAGNOSTICS_PER_FILE:
            facts.diagnostics.append(
                ParseDiagnostic(
                    line, "Suppressed", "suppressed: non-operation client member " + ref
                )
            )

    def emit(facts: FileFacts, caller: str, ref: str, line: int) -> None:
        tag = (facts.path, caller, line, ref)
        if tag in emitted:
            return
        emitted.add(tag)
        if owner_of(sm, facts.path)[0] == "client":
            facts.suppressed_calls += 1
            return
        hits = resolve(ref, facts.path, caller)
        method = ref.rsplit(".", 1)[-1]
        receiver = ref.rsplit(".", 1)[0] if "." in ref else ""
        candidates = resolver.receiver_types(receiver, facts.path, caller) if receiver else []
        if method.startswith("select_header_") or method in {
            "sanitize_for_serialization",
            "deserialize",
            "parameters_to_tuples",
            "files_parameters",
            "update_params_for_auth",
        }:
            if any(clients_for_source(typ) for typ in candidates):
                suppress(facts, ref, line)
                return
        clients: dict[tuple[str, str], tuple[Any, str]] = {}
        for candidate in candidates:
            for client in clients_for_source(candidate):
                package = next(
                    (p for p in client.packages if candidate == p or candidate.startswith(p + ".")),
                    "",
                )
                identity = (client_owners[id(client)], client.id)
                previous = clients.get(identity)
                if package or previous is None:
                    clients[identity] = (client, package)
        if hits and any(h in http_syms for h in hits):
            facts.calls.append(CallFact(caller, ref, line))
            # The CALLS edge reaches the source method's literal HTTP fact.
            # Do not also emit a shortcut CALLS_API edge at this site: that
            # would count the same operation twice in the handler closure.
        elif clients:
            # Source-visible models and factories are bindings, never operations.
            model = any(re.search(r"(?:^|\.)(?:model|models)(?:\.|$)", typ) for typ in candidates)
            model |= any(
                resolver.facts[p].class_roles.get(q) == "model"
                for typ in candidates
                for p, q in resolver.resolve(typ, facts.path, caller)
            )
            factory = bool(re.fullmatch(r"get\w*Api|getInstance", method)) and any(
                resolver.facts[p].returns.get(q, "").endswith(("Api", "API")) for p, q in hits
            )
            # The endpoint inventory is supplied below by _link_http; defer
            # fallback accessor suppression until operation IDs are available.
            if model or factory:
                suppress(facts, ref, line)
                return
            indexed_classes = [
                hit
                for candidate in candidates
                for hit in resolver.resolve(candidate, facts.path, caller)
                if client_for_factory(candidate) is not None
                and resolver.symbols[hit].kind == "CLASS"
                and resolver.symbols[hit].name.endswith("Api")
            ]
            if indexed_classes and not resolver._methods(indexed_classes, method, set()):
                suppress(facts, ref, line)
                return
            for client, package in clients.values():
                if (
                    (
                        facts.language == "java"
                        and method
                        in {
                            "toLowerCase",
                            "toUpperCase",
                            "isEmpty",
                            "getOrDefault",
                            "toString",
                            "hashCode",
                            "equals",
                        }
                    )
                    or method[:1].isupper()
                    or any(
                        part in ref.split(".")
                        for part in {"rest", "models", "configuration", "ApiException"}
                    )
                ):
                    suppress(facts, ref, line)
                    continue
                facts.http_calls.append(
                    HttpFact(
                        caller,
                        line,
                        "",
                        "",
                        "call-through",
                        (f"{package}.{method}" if package else ref),
                        client_id=client.id,
                        client_owner=client_owners[id(client)],
                        binding_rule="factory:inferred-return"
                        if resolver.factory_evidence(receiver, facts.path, caller)
                        else (
                            "factory:declared"
                            if any(
                                candidate == factory or candidate.endswith("." + factory)
                                for candidate in candidates
                                for factory, _ in factories
                            )
                            else ""
                        ),
                        detail="; ".join(resolver.factory_evidence(receiver, facts.path, caller))
                        + " "
                        + resolver.binding_trace(receiver, facts.path, caller),
                        ambiguous=len(clients) > 1,
                    )
                )
        elif hits:
            facts.calls.append(CallFact(caller, ref, line))
        # Unresolvable dotted refs emit nothing: never guess a client.

    for facts in all_facts:
        if dirty_paths is not None and facts.path not in dirty_paths:
            continue
        _check_cancelled(cancelled)
        for c in list(facts.calls):
            if not c.caller:
                continue
            caller_key = (facts.path, c.caller)
            # Direct invocation on a factory-bound receiver.
            if "." in c.called:
                receiver = c.called.rsplit(".", 1)[0].split(".")[0]
                source = bindings_at(facts.path, c.caller).get(receiver, "")
                source = resolver.canonical(source, facts.path) if source else ""
                types = resolver.receiver_types(c.called.rsplit(".", 1)[0], facts.path, c.caller)
                leaf = c.called.rsplit(".", 1)[-1]
                providers = {sdk_provider(typ) for typ in types} - {""}
                if len(providers) == 1 and not any(
                    h.caller == c.caller and h.line == c.line for h in facts.http_calls
                ):
                    facts.http_calls.append(
                        HttpFact(
                            c.caller,
                            c.line,
                            via="sdk",
                            call_name=c.called,
                            provider=next(iter(providers)),
                        )
                    )
                if (
                    leaf in {"get", "post", "put", "patch", "delete", "head", "options", "request"}
                    and any(
                        typ
                        in {
                            "requests.Session",
                            "requests.sessions.Session",
                            "httpx.Client",
                            "httpx.AsyncClient",
                            "aiohttp.ClientSession",
                            "urllib3.PoolManager",
                        }
                        for typ in types
                    )
                    and not any(h.caller == c.caller and h.line == c.line for h in facts.http_calls)
                ):
                    method = (
                        c.arguments.get("method", c.arguments.get("0", ""))
                        if leaf == "request"
                        else leaf.upper()
                    )
                    url = c.arguments.get(
                        "url", c.arguments.get("1" if leaf == "request" else "0", "")
                    )
                    if method.upper() not in {
                        "GET",
                        "POST",
                        "PUT",
                        "PATCH",
                        "DELETE",
                        "HEAD",
                        "OPTIONS",
                    }:
                        method = ""
                    facts.http_calls.append(
                        HttpFact(c.caller, c.line, method.upper(), url, "http-client", c.called)
                    )
                if any(client_for_factory(candidate) is not None for candidate in [source, *types]):
                    emit(facts, c.caller, c.called, c.line)
            # Passing a callable into a transparent wrapper.
            if not c.callable_args:
                continue
            for callee in resolve(c.called, facts.path, c.caller):
                callee_params = params_of.get(callee, [])
                for idx, ref in c.callable_args:
                    if idx >= len(callee_params):
                        continue
                    if callee_params[idx] not in transparent.get(callee, set()):
                        continue
                    if ref in params_of.get(caller_key, []):
                        continue  # forwarding our own param; handled upstream
                    emit(facts, c.caller, ref, c.line)

    # Resolve name-only coverage diagnostics after type/client synthesis, so a
    # declared receiver is not incorrectly reported as untyped.
    for facts in all_facts:
        if dirty_paths is not None and facts.path not in dirty_paths:
            continue
        facts.diagnostics = [d for d in facts.diagnostics if d.category != "untyped-client"]
        if owner_of(sm, facts.path)[0] == "client":
            continue
        http_sites = {(h.caller, h.line) for h in facts.http_calls}
        unknown = []
        for call in facts.calls:
            receiver, dot, _ = call.called.rpartition(".")
            if not dot or not any(
                t in receiver.rsplit(".", 1)[-1].lower() for t in ("client", "_api")
            ):
                continue
            if (call.caller, call.line) in http_sites or resolve(
                call.called, facts.path, call.caller
            ):
                continue
            if any(
                clients_for_source(t)
                for t in resolver.receiver_types(receiver, facts.path, call.caller)
            ):
                continue
            unknown.append(call)
        if unknown:
            facts.diagnostics.append(
                ParseDiagnostic(
                    unknown[0].line,
                    "untyped-client",
                    f"untyped client receivers: {len(unknown)}; "
                    + ", ".join(sorted({c.called for c in unknown})[:3]),
                )
            )


class _CallResolver:
    """Resolve names from lexical and import evidence, never arbitrary leaves."""

    def __init__(self, all_facts: list[FileFacts], owners: dict[str, tuple[str, str]]) -> None:
        self.facts = {f.path: f for f in all_facts}
        self.owners = owners
        self.diagnostics: set[str] = set()
        self.symbols = {(f.path, s.qualified): s for f in all_facts for s in f.symbols}
        self.names: dict[str, list[tuple[str, str]]] = {}
        self.qualified_names: dict[str, list[tuple[str, str]]] = {}
        self.modules: dict[str, list[str]] = {}
        self.cache: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
        self.bindings: dict[tuple[str, str], dict[str, str]] = {}
        self.binding_values: dict[tuple[str, str, str], list[str]] = {}
        self.type_cache: dict[tuple[str, str, str], list[str]] = {}
        self.type_traces: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
        self.type_active: set[tuple[str, str, str]] = set()
        self.call_sites: dict[tuple[str, str], list[tuple[FileFacts, CallFact]]] | None = None
        self.settings_cache: dict[tuple[str, str], str] = {}
        for f in all_facts:
            module = f.path.rsplit(".", 1)[0].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[:-9]
            for alias in _suffixes(module):
                self.modules.setdefault(alias, []).append(f.path)
            for binding in f.bindings:
                self.binding_values.setdefault((f.path, binding.caller, binding.var), []).append(
                    binding.source
                )
                self.bindings.setdefault((f.path, binding.caller), {})[binding.var] = binding.source
            for sym in f.symbols:
                self.names.setdefault(sym.qualified, []).append((f.path, sym.qualified))
                if f.package and f.language == "java":
                    self.qualified_names.setdefault(f.package + "." + sym.qualified, []).append(
                        (f.path, sym.qualified)
                    )

    def django_settings(self, path: str) -> str:
        owner = self.owners.get(path)
        if not owner or owner[0] != "service":
            return ""
        if owner in self.settings_cache:
            return self.settings_cache[owner]
        self.settings_cache[owner] = ""
        owned = [f for p, f in self.facts.items() if self.owners.get(p) == owner]
        modules = {
            value
            for f in owned
            if Path(f.path).name in {"manage.py", "wsgi.py", "asgi.py"}
            for value in f.constants.get("__django_settings_module__", [])
        }
        candidates = {
            p
            for module in modules
            for p in self.modules.get(module, [])
            if self.owners.get(p) == owner
        }
        if not modules:
            candidates = {
                f.path
                for f in owned
                if f.path in {"settings.py", "settings/__init__.py"}
                or f.path.endswith(("/settings.py", "/settings/__init__.py"))
            }
        if len(candidates) == 1 and len(modules) <= 1:
            p = next(iter(candidates))
            module = p.rsplit(".", 1)[0].replace("/", ".").removesuffix(".__init__")
            self.settings_cache[owner] = module
            return module
        if len(candidates) > 1 or len(modules) > 1:
            self.diagnostics.add(
                f"{path}: django-settings:ambiguous: " + ", ".join(sorted(candidates or modules))
            )
        return ""

    @staticmethod
    def scopes(caller: str) -> list[str]:
        result = [caller]
        while caller:
            caller = caller.rsplit(".", 1)[0] if "." in caller else ""
            result.append(caller)
        return result

    def canonical(self, ref: str, path: str) -> str:
        head, dot, tail = ref.partition(".")
        origin = self.facts[path].import_map.get(head, head)
        if origin == "django.conf.settings":
            origin = self.django_settings(path) or origin
        return origin + (dot + tail if dot else "")

    def receiver_types(self, receiver: str, path: str, caller: str, depth: int = 0) -> list[str]:
        key = (path, caller, receiver)
        if depth > 8 or key in self.type_active:
            return []
        if key in self.type_cache:
            return self.type_cache[key]
        self.type_active.add(key)
        try:
            out = self._receiver_types(receiver, path, caller, depth)
        finally:
            self.type_active.discard(key)
        result = list(dict.fromkeys(out))
        # Depth-limited partial results depend on the traversal budget.
        if depth == 0:
            self.type_cache[key] = result
        return result

    def _receiver_types(self, receiver: str, path: str, caller: str, depth: int) -> list[str]:
        def follow(ref: str, p: str = path, scope: str = caller) -> list[str]:
            self.type_traces.setdefault((path, caller, receiver), set()).add((p, scope, ref))
            return self.receiver_types(ref, p, scope, depth + 1)

        if receiver.startswith("literal:"):
            return [receiver[8:]]
        if receiver.startswith("default:"):
            primary, fallback = json.loads(receiver[8:])
            return follow(primary) or follow(fallback)
        if receiver.startswith("union:"):
            return [t for ref in json.loads(receiver[6:]) for t in follow(ref)]
        if receiver.startswith("param:"):
            match = re.fullmatch(r"param:([^@]+)@([^\[]+)(?:\[([^]]+)\])?", receiver)
            if not match:
                return []
            param, function, dict_key = match.groups()
            symbol = self.symbols.get((path, function))
            if symbol is None:
                return []
            if self.call_sites is None:
                self.call_sites = {}
                for file in self.facts.values():
                    for call in file.calls:
                        for target in self.resolve(call.called, file.path, call.caller):
                            self.call_sites.setdefault(target, []).append((file, call))
                            if self.symbols[target].kind == "CLASS":
                                for init in self._methods([target], "__init__", set()):
                                    self.call_sites.setdefault(init, []).append((file, call))
            annotation = self.bindings.get((path, function), {}).get(param, "")
            out = follow(annotation, path, function) if annotation else []
            for file, call in self.call_sites.get((path, function), []):
                args = dict(call.arg_refs)
                offset = int(bool(symbol.params and symbol.params[0] in {"self", "cls"}))
                position = symbol.params.index(param) - offset if param in symbol.params else -1
                ref = args.get(param, args.get(position, ""))
                if dict_key:
                    dict_ref = ref or args.get("**", "")
                    for _ in range(8):
                        if dict_ref.startswith("dict:"):
                            break
                        bound = next(
                            (
                                self.bindings.get((file.path, scope), {}).get(dict_ref, "")
                                for scope in self.scopes(call.caller)
                                if self.bindings.get((file.path, scope), {}).get(dict_ref)
                            ),
                            "",
                        )
                        if not bound or bound == dict_ref:
                            break
                        dict_ref = bound
                    ref = (
                        json.loads(dict_ref[5:]).get(dict_key, "")
                        if dict_ref.startswith("dict:")
                        else args.get(dict_key, "")
                    )
                if ref:
                    out.extend(follow(ref, file.path, call.caller))
            return out
        invocation = receiver.startswith("call:")
        if invocation:
            receiver = receiver[5:]
        for scope in self.scopes(caller):
            values = self.binding_values.get((path, scope, receiver), [])
            # Class body attributes are written without self/cls.
            if not values and receiver.startswith(("self.", "cls.")):
                values = self.binding_values.get((path, scope, receiver.split(".", 1)[1]), [])
            if values:
                return [t for ref in values for t in follow(ref, path, scope)]
        if receiver.startswith(("self.", "cls.")) and "." in caller:
            cls = caller.rsplit(".", 1)[0]
            queue = [(path, cls)]
            seen: set[tuple[str, str]] = set()
            while queue:
                p, q = queue.pop()
                if (p, q) in seen:
                    continue
                seen.add((p, q))
                values = self.binding_values.get((p, q, receiver), []) or self.binding_values.get(
                    (p, q, receiver.split(".", 1)[1]), []
                )
                if values:
                    return [t for ref in values for t in follow(ref, p, q)]
                for base in self.facts[p].superclasses.get(q, []):
                    queue.extend(self.resolve(base, p, q))
        source = receiver
        if source in ("self", "cls", "this") and "." in caller:
            source = caller.rsplit(".", 1)[0]
        canonical = self.canonical(source, path)
        out = [canonical]
        receiver_root, dot, member = source.rpartition(".")
        if dot:
            root_types = follow(receiver_root)
            # A member invocation/result is not the receiver instance. A method's
            # declared return type below is the only source of result bindings.
            if any(typ != receiver_root for typ in root_types) and invocation:
                out = []
        # Imported module globals have bindings, even when they aren't symbols.
        parts = canonical.split(".")
        for i in range(len(parts) - 1, 0, -1):
            for module_path in self._owned_paths(self.modules.get(".".join(parts[:i]), []), path):
                name = ".".join(parts[i:])
                if (module_path, "", name) in self.binding_values:
                    out.extend(follow(name, module_path, ""))
        for target_path, name in self.resolve(source, path, caller):
            facts = self.facts[target_path]
            if self.symbols[(target_path, name)].kind == "CLASS":
                module = (
                    facts.package
                    if facts.language == "java"
                    else target_path.rsplit(".", 1)[0].replace("/", ".").removesuffix(".__init__")
                )
                qualified = module + "." + name if module else name
                if canonical in out:
                    out.remove(canonical)
                out.append(qualified)
            annotation = facts.returns.get(name, "")
            returned = [annotation] if annotation else facts.returns_inferred.get(name, [])
            if (
                invocation
                and returned
                and self.symbols[(target_path, name)].kind != "CLASS"
                and canonical in out
            ):
                out.remove(canonical)
            for parent in returned + facts.superclasses.get(name, []):
                out.extend(follow(parent, target_path, name))
        return out

    def binding_trace(self, receiver: str, path: str, caller: str) -> str:
        queue = [(path, caller, receiver)]
        seen: set[tuple[str, str, str]] = set()
        parts = []
        while queue and len(seen) < 32:
            key = queue.pop(0)
            if key in seen:
                continue
            seen.add(key)
            parts.append(f"{key[0]}:{key[1]}:{key[2]}")
            queue.extend(sorted(self.type_traces.get(key, set())))
        return " ← ".join(parts)

    def factory_evidence(self, receiver: str, path: str, caller: str) -> list[str]:
        queue = [(path, caller, receiver)]
        seen: set[tuple[str, str, str]] = set()
        evidence: set[str] = set()
        while queue and len(seen) < 64:
            key = queue.pop(0)
            if key in seen:
                continue
            seen.add(key)
            for fact in self.facts[key[0]].factory_returns:
                if fact.function == key[1]:
                    evidence.add(f"{key[0]}:{fact.line} return {fact.package_ref}")
            queue.extend(sorted(self.type_traces.get(key, set())))
        return sorted(evidence)

    def _owned_paths(self, candidates: list[str], path: str) -> list[str]:
        """Rank aliases by declared ownership before directory proximity."""
        owner = self.owners.get(path, ("unowned", ""))
        ranked: dict[int, list[str]] = {}
        blocked: list[str] = []
        for candidate in candidates:
            kind, service = self.owners.get(candidate, ("unowned", ""))
            if kind in {"service", "client"} and owner[1] and service != owner[1]:
                blocked.append(candidate)
                continue
            rank = 0 if service and service == owner[1] else 1 if kind == "shared" else 2
            ranked.setdefault(rank, []).append(candidate)
        if not ranked:
            if blocked:
                self.diagnostics.add(
                    f"{path}: unresolved cross-service module candidates: "
                    + ", ".join(sorted(blocked))
                )
            return []
        hits = ranked[min(ranked)]
        same = [p for p in hits if Path(p).parent == Path(path).parent]
        return same or hits

    def _qualified(self, ref: str, path: str) -> list[tuple[str, str]]:
        if ref in self.qualified_names:
            return [
                hit
                for hit in self.qualified_names[ref]
                if hit[0] in self._owned_paths([p for p, _ in self.qualified_names[ref]], path)
            ]
        parts = ref.split(".")
        for i in range(len(parts) - 1, 0, -1):
            modules = self.modules.get(".".join(parts[:i]), [])
            if not modules:
                continue
            modules = self._owned_paths(modules, path)
            name = ".".join(parts[i:])
            hits = [(p, name) for p in modules if (p, name) in self.symbols]
            if hits:
                return hits
        return []

    def _methods(
        self, classes: list[tuple[str, str]], member: str, seen: set[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        out = []
        for path, cls in classes:
            if (path, cls) in seen:
                continue
            seen.add((path, cls))
            if (path, cls + "." + member) in self.symbols:
                out.append((path, cls + "." + member))
                continue
            for base in self.facts[path].superclasses.get(cls, []):
                out.extend(self._methods(self.resolve(base, path, cls), member, seen))
        return out

    def resolve(
        self, ref: str, path: str, caller: str = "", depth: int = 0
    ) -> list[tuple[str, str]]:
        key = (ref, path, caller)
        if key in self.cache:
            return self.cache[key]
        if depth > 8 or path not in self.facts:
            return []
        self.cache[key] = []
        original = ref
        if "()." in ref:
            factory, member = ref.rsplit("().", 1)
            classes = []
            for factory_path, factory_name in self.resolve(factory, path, caller, depth + 1):
                annotation = self.facts[factory_path].returns.get(factory_name, "")
                if annotation:
                    classes.extend(self.resolve(annotation, factory_path, factory_name, depth + 1))
            self.cache[key] = self._methods(classes, member, set())
            return self.cache[key]
        ref = self.canonical(ref, path)
        out = self._qualified(ref, path)
        if not out and "." in original:
            receiver, member = original.rsplit(".", 1)
            classes = []
            if receiver in ("self", "cls", "this") and "." in caller:
                classes = [(path, caller.rsplit(".", 1)[0])]
            elif receiver == "super" and "." in caller:
                cls = caller.rsplit(".", 1)[0]
                for base in self.facts[path].superclasses.get(cls, []):
                    classes.extend(self.resolve(base, path, cls, depth + 1))
            else:
                scopes = self.scopes(caller)
                source = next(
                    (
                        self.bindings.get((path, scope), {}).get(receiver, "")
                        for scope in scopes
                        if self.bindings.get((path, scope), {}).get(receiver)
                    ),
                    "",
                )
                if source:
                    classes = self.resolve(source, path, caller, depth + 1)
                    returned = []
                    for target_path, target_name in classes:
                        target_facts = self.facts[target_path]
                        annotation = target_facts.returns.get(target_name, "")
                        for value in (
                            [annotation]
                            if annotation
                            else target_facts.returns_inferred.get(target_name, [])
                        ):
                            returned.extend(
                                self.resolve(
                                    value.removeprefix("call:"), target_path, target_name, depth + 1
                                )
                            )
                    classes = returned or classes
                else:
                    classes = self.resolve(receiver, path, caller, depth + 1)
            out = self._methods(classes, member, set())
            if not out and (path, original) in self.symbols:
                out = [(path, original)]
        elif not out and "." not in original:
            scope = caller
            while scope and not out:
                qualified = scope + "." + original
                if (path, qualified) in self.symbols:
                    out = [(path, qualified)]
                elif (path, scope) in self.symbols and self.symbols[(path, scope)].kind == "CLASS":
                    out = self._methods([(path, scope)], original, set())
                scope = scope.rsplit(".", 1)[0] if "." in scope else ""
            if not out and (path, original) in self.symbols:
                out = [(path, original)]
            if not out and ref == original:
                candidates = [
                    k
                    for k in self.names.get(original, [])
                    if self.symbols[k].kind != "METHOD"
                    and self.facts[k[0]].language == self.facts[path].language
                ]
                allowed = self._owned_paths([p for p, _ in candidates], path)
                candidates = [k for k in candidates if k[0] in allowed]
                service = self.owners.get(path, ("", ""))[1]
                same_package = [
                    k
                    for k in candidates
                    if self.facts[path].language in {"java", "go"}
                    and (
                        (
                            self.facts[path].package
                            and self.facts[k[0]].package == self.facts[path].package
                        )
                        or Path(k[0]).parent == Path(path).parent
                    )
                ]
                candidates = same_package or [
                    k
                    for k in candidates
                    if service and self.owners.get(k[0], ("", ""))[1] == service
                ]
                if len(candidates) == 1:
                    out = candidates
        self.cache[key] = list(
            dict.fromkeys(
                hit for hit in out if self.facts[hit[0]].language == self.facts[path].language
            )
        )
        return self.cache[key]


def _add_calls(
    snap: GraphSnapshot,
    repo_id: str,
    all_facts: list[FileFacts],
    owners: dict[str, tuple[str, str]],
    dirty_paths: set[str] | None = None,
) -> None:
    resolver = _CallResolver(all_facts, owners)
    subclasses: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for facts in all_facts:
        for cls, bases in facts.superclasses.items():
            for base in bases:
                for parent in resolver.resolve(base, facts.path, cls):
                    if owners.get(parent[0], ("", ""))[1] == owners.get(facts.path, ("", ""))[1]:
                        subclasses.setdefault(parent, []).append((facts.path, cls))
    for facts in all_facts:
        if dirty_paths is not None and facts.path not in dirty_paths:
            continue
        for call in facts.calls:
            caller_id = _sym_id(repo_id, facts.path, call.caller)
            if not call.caller or caller_id not in snap.nodes:
                continue
            hits = resolver.resolve(call.called, facts.path, call.caller)
            dispatch = False
            if not hits and "." in call.called:
                receiver, member = call.called.rsplit(".", 1)
                dispatch = True
                for candidate in resolver.receiver_types(receiver, facts.path, call.caller):
                    hits.extend(
                        resolver._methods(
                            resolver.resolve(candidate, facts.path, call.caller), member, set()
                        )
                    )
            cha = call.rule == "dispatch:cha"
            if "." in call.called:
                receiver, member = call.called.rsplit(".", 1)
                symbol = resolver.symbols.get((facts.path, call.caller))
                if symbol and receiver in symbol.params:
                    annotation = resolver.bindings.get((facts.path, call.caller), {}).get(
                        receiver, ""
                    )
                    queue = (
                        list(resolver.resolve(annotation, facts.path, call.caller))
                        if annotation
                        else []
                    )
                    seen: set[tuple[str, str]] = set()
                    while queue:
                        class_key = queue.pop()
                        if class_key in seen:
                            continue
                        seen.add(class_key)
                        children = subclasses.get(class_key, [])
                        queue.extend(children)
                        if children:
                            cha = True
                            hits.extend(resolver._methods(children, member, set()))
            targets = [_sym_id(repo_id, p, q) for p, q in dict.fromkeys(hits)]
            targets = [t for t in targets if t != caller_id and t in snap.nodes]
            conf = Confidence.EXTRACTED if len(targets) == 1 and not cha else Confidence.AMBIGUOUS
            if len(targets) == 1 and (dispatch or call.rule) and not cha:
                conf = Confidence.INFERRED
            if len(targets) > 25:
                snap.diagnostics.append(
                    f"graph-unsound: {facts.path}:{call.line}: dispatch targets truncated at 25 "
                    f"({len(targets)} candidates)"
                )
            for target in targets[:25]:
                snap.add_edge(
                    GraphEdge(
                        from_id=caller_id,
                        to_id=target,
                        kind=EdgeKind.CALLS,
                        confidence=conf,
                        evidence=Evidence(
                            file=facts.path,
                            line=call.line,
                            rule=call.rule
                            or ("dispatch:cha" if cha else "")
                            or ("dispatch:factory-return" if dispatch else "call:name-resolution"),
                        ),
                    )
                )

    from axon.v2.framework_hooks import add_framework_calls

    add_framework_calls(snap, repo_id, resolver, dirty_paths)

    snap.diagnostics.extend(sorted(resolver.diagnostics))


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
    snap: GraphSnapshot,
    repo_id: str,
    sm: ServiceMap,
    all_facts: list[FileFacts],
    sources: dict[str, str] | None = None,
) -> None:
    index = EndpointIndex(snap)
    first_edge = len(snap.edges)
    source_suppressed: dict[str, int] = {}
    accessor_suppressed: dict[str, int] = {}
    for facts in all_facts:
        source_kind, source_owner = owner_of(sm, facts.path)
        if source_kind == "client":
            source_suppressed[source_owner] = (
                source_suppressed.get(source_owner, 0) + facts.suppressed_calls
            )
        precise_calls = {(h.caller, h.line) for h in facts.http_calls if h.via == "call-through"}
        suppressed_lines = {d.line for d in facts.diagnostics if d.category == "Suppressed"}
        for fact in facts.http_calls:
            if source_kind == "client" and not (
                fact.url and url_shaped(fact.url) and fact.via not in {"call-through", "sdk"}
            ):
                source_suppressed[source_owner] = source_suppressed.get(source_owner, 0) + 1
                continue
            if fact.via == "generated-client" and (
                (fact.caller, fact.line) in precise_calls or fact.line in suppressed_lines
            ):
                continue
            caller_id = _sym_id(repo_id, facts.path, fact.caller)
            if caller_id not in snap.nodes:
                continue
            if fact.via == "generated-source":
                fact.callee = fact.callee or f"{facts.path}::{fact.caller}"
            if fact.via == "generated-source" and not fact.client_id and source_kind == "client":
                identities = [
                    (svc.id, c.id)
                    for svc in sm.services
                    for c in svc.clients
                    if any(_matches(facts.path, path) for path in c.paths)
                ]
                if len(identities) == 1:
                    fact.client_owner, fact.client_id = identities[0]
            if fact.via == "sdk":
                target = external_node_id(
                    "SDK", fact.provider + ":" + fact.call_name.rsplit(".", 1)[-1]
                )
                snap.add_node(
                    GraphNode(
                        id=target,
                        kind=NodeKind.EXTERNAL_API,
                        name=fact.call_name.rsplit(".", 1)[-1],
                        props={
                            "kind": "sdk",
                            "provider": fact.provider,
                            "operation": fact.call_name,
                        },
                    )
                )
                snap.add_edge(
                    GraphEdge(
                        from_id=caller_id,
                        to_id=target,
                        kind=EdgeKind.CALLS_API,
                        evidence=Evidence(file=facts.path, line=fact.line, rule="external:sdk"),
                        props={"kind": "sdk", "provider": fact.provider},
                    )
                )
                continue
            if fact.url and not url_shaped(fact.url):
                snap.diagnostics.append(f"{facts.path}:{fact.line}: suppressed: non-url literal")
                continue
            if fact.via in {"call-through", "generated-client"} and not fact.url:
                leaf = fact.call_name.rsplit(".", 1)[-1]
                source_call = next(
                    (c for c in facts.calls if c.caller == fact.caller and c.line == fact.line),
                    None,
                )
                count = (
                    source_call.argument_count
                    if source_call and source_call.argument_count >= 0
                    else len(source_call.arguments)
                    if source_call
                    else 0
                )
                accessor = bool(re.fullmatch(r"(?:get|is|has)[A-Z]\w*", leaf)) and count == 0
                accessor |= bool(re.fullmatch(r"set[A-Z]\w*", leaf)) and count == 1
                accessor |= bool(
                    re.fullmatch(
                        r"builder|build|with[A-Z]\w*|getInstance|toString|hashCode|equals|getClass|wait|notify|notifyAll",
                        leaf,
                    )
                )
                if accessor and not operation_candidates(fact, index, sm, repo_id):
                    accessor_suppressed[source_owner] = accessor_suppressed.get(source_owner, 0) + 1
                    continue
            resolved = resolve_http(
                fact, index, sm, repo_id, snap.nodes[caller_id].service_id, sources
            )
            if resolved is None:
                template = url_path(fact.url) or fact.call_name or "unknown"
                base_ref = base_reference(fact.url)
                # Preserve symbolic ownership evidence and distinct literal hosts.
                if base_ref:
                    template = fact.url.split("?")[0].split("#")[0]
                dynamic = bool(fact.url) and route_specificity(url_path(fact.url))[0] == 0
                candidates = (
                    []
                    if dynamic
                    else index.by_method_path(fact.method or "ANY", url_path(fact.url))
                )
                if (
                    not dynamic
                    and not candidates
                    and base_ref.startswith(("env:", "setting:", "attr:"))
                    and sm.index.path_suffix_fallback
                ):
                    candidates = index.by_path_suffix(fact.method, url_path(fact.url))
                path_ambiguous = len({(h.repo_id, h.service_id) for h in candidates}) > 1
                operations = (
                    operation_candidates(fact, index, sm, repo_id) if not path_ambiguous else []
                )
                operation_ambiguous = len(operations) > 1
                dedupe_suffix = operation_ambiguous and bool(
                    re.search(r"_\d+$", _operation_base(fact.call_name))
                )
                candidate_ids = (
                    sorted({h.id for h in candidates})
                    if path_ambiguous
                    else (sorted({h.id for h in operations}) if operation_ambiguous else [])
                )
                ext_id = external_node_id(fact.method or "ANY", template)
                snap.add_node(
                    GraphNode(
                        id=ext_id,
                        kind=NodeKind.EXTERNAL_API,
                        name=f"{fact.method or 'ANY'} {template}".strip(),
                        props={
                            "method": fact.method,
                            "url": fact.url,
                            "call_name": fact.call_name,
                            "base_ref": base_ref,
                            "candidates": json.dumps(candidate_ids),
                        },
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
                            rule="unresolved:dynamic-path"
                            if dynamic
                            else "unresolved:path-ambiguous"
                            if path_ambiguous
                            else f"client:{fact.client_id}(dedupe-suffix)"
                            if dedupe_suffix and fact.client_id
                            else "unresolved:operation-ambiguous"
                            if operation_ambiguous
                            else f"unresolved:{fact.via}",
                            detail=fact.call_name,
                        ),
                        props={
                            "method": fact.method,
                            "url": fact.url,
                            "call_name": fact.call_name,
                            "via": fact.via,
                            "base_ref": base_ref,
                            "candidates": json.dumps(candidate_ids),
                        },
                    )
                )
                snap.unresolved.append(
                    f"unresolved outbound call {facts.path}:{fact.line} "
                    f"{fact.call_name} {fact.url}".strip()
                )
            else:
                target_id, conf, rule = resolved
                if fact.ambiguous and conf != Confidence.MANUAL:
                    conf = Confidence.AMBIGUOUS
                    rule += "(many-binding)"
                if target_id not in snap.nodes:
                    snap.unresolved.append(
                        f"manual link target not indexed yet: {target_id} "
                        f"(from {facts.path}:{fact.line})"
                    )
                    if conf != Confidence.MANUAL:
                        continue
                    template = target_id
                    target_id = external_node_id(fact.method or "ANY", template)
                    snap.add_node(
                        GraphNode(
                            id=target_id,
                            kind=NodeKind.EXTERNAL_API,
                            name=template,
                            props={"target_endpoint": template, "call_name": fact.call_name},
                        )
                    )
                snap.add_edge(
                    GraphEdge(
                        from_id=caller_id,
                        to_id=target_id,
                        kind=EdgeKind.CALLS_API,
                        confidence=conf,
                        evidence=Evidence(
                            file=facts.path,
                            line=fact.line,
                            rule=rule if conf == Confidence.MANUAL else fact.binding_rule or rule,
                            detail=fact.detail or fact.call_name,
                        ),
                        props={
                            "method": fact.method,
                            "url": fact.url,
                            "call_name": fact.call_name,
                            "via": fact.via,
                            "client_id": fact.client_id,
                            "callee": fact.callee,
                            **(
                                {
                                    "source_file": rule[7:].split("#")[0],
                                    "entry_id": rule.partition("#")[2],
                                }
                                if conf == Confidence.MANUAL
                                else {}
                            ),
                            "base_ref": fact.base_ref or base_reference(fact.url),
                            **({"bound": json.dumps(fact.bound)} if fact.bound else {}),
                        },
                    )
                )
    for service, count in sorted(accessor_suppressed.items()):
        snap.diagnostics.append(f"{service}: suppressed: non-operation model accessor: {count}")
    for service, count in sorted(source_suppressed.items()):
        snap.diagnostics.append(f"{service}: client-source calls suppressed: {count}")
    reconcile_path_fallbacks(snap, snap.edges[first_edge:], sm)
