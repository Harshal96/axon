"""Conservative incremental resolution with transactional graph/state publication.

Service roots are an isolation boundary. Until shared receiver dependencies can
be proven individually, repositories with shared/unowned/client source fall back
to full resolution. Endpoint linking always runs against the new complete set.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from axon.v2.facts import FileFacts
from axon.v2.factscache import cache_key
from axon.v2.graph import GraphSnapshot
from axon.v2.graph_delta import GraphDelta, edge_key, replace_repositories
from axon.v2.indexer import PARSER_VERSIONS, IndexTimings, _check_cancelled, index_repo
from axon.v2.indexstate import FileState, IndexState, StateStore, fallback_reason, git_changes
from axon.v2.indexstate import interface_hash as exported_hash
from axon.v2.models import NodeKind, repo_node_id
from axon.v2.revision import checkout_commit, checkout_dirty
from axon.v2.servicemap import ServiceMap, load_service_map, owner_of, service_map_fingerprint
from axon.v2.store import DeltaNeedsFullSave, GraphStore

logger = logging.getLogger(__name__)
_LOCKS_GUARD = threading.Lock()
_REPO_LOCKS: dict[str, threading.Lock] = {}


def index_and_publish(
    root: Path, store: GraphStore, **kwargs: Any
) -> tuple[GraphSnapshot, ServiceMap, dict[str, Any]]:
    # Graph and state identities use the declared repository id, even when two
    # server checkouts have different physical paths.
    identity = load_service_map(root).repo
    with _LOCKS_GUARD:
        lock = _REPO_LOCKS.setdefault(identity, threading.Lock())
    with lock:
        return _index_and_publish(root, store, **kwargs)


def _index_and_publish(
    root: Path,
    store: GraphStore,
    *,
    commit: str = "",
    base_commit: str = "",
    force: bool = False,
    timings: IndexTimings | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    cancelled: threading.Event | None = None,
    state_dir: str | Path | None = None,
) -> tuple[GraphSnapshot, ServiceMap, dict[str, Any]]:
    root = root.resolve()
    sm = load_service_map(root)
    fingerprint = service_map_fingerprint(root)
    from axon.v2.fingerprints import build_versions

    parser_version = build_versions()
    state_store = StateStore(sm.repo, state_dir)
    current = store.load()
    previous = state_store.load(current)
    dirty = checkout_dirty(root)
    head = checkout_commit(root)
    changes = git_changes(root, previous.commit, commit) if previous and commit else None
    changed = {p for change in (changes or []) for p in (change.path, change.old_path) if p}
    reason = fallback_reason(
        previous, fingerprint, parser_version, len(changed), len(previous.files) if previous else 0
    )
    # Retrying degraded parses is scheduling, not a repository edit. It must
    # not inflate the Git change ratio and force permanent full rebuilds.
    if previous:
        changed.update(
            p
            for p, entry in previous.files.items()
            if any(d.severity == "file-degraded" for d in entry.facts.diagnostics)
        )
    if force:
        reason = "forced"
    elif dirty is not False:
        reason = "dirty-or-unknown-checkout"
    elif commit and head and commit != head:
        reason = "commit-checkout-mismatch"
    elif previous is not None and reason is None:
        persisted = previous.snapshot.nodes.get(repo_node_id(sm.repo))
        if current.nodes.get(repo_node_id(sm.repo)) != persisted:
            reason = "graph-state-mismatch"
        elif base_commit and base_commit != previous.commit:
            reason = "base-commit-mismatch"
        elif changes is None:
            reason = "git-diff-unavailable"
        elif (persisted is not None and persisted.props.get("graph_unsound") == "true") or any(
            "graph-unsound:" in d for d in previous.snapshot.diagnostics
        ):
            reason = "diagnostics-require-full"
        elif any(
            node.props.get("owner_kind") != "service"
            for node in previous.snapshot.by_kind(NodeKind.SOURCE_FILE)
        ):
            reason = "cross-owner-dependencies"
        elif any(
            owner_of(sm, p)[0] != "service"
            for p, entry in previous.files.items()
            if entry.facts.language in {"python", "java", "go"}
        ):
            reason = "cross-owner-dependencies"
        elif any(Path(p).suffix not in PARSER_VERSIONS for p in changed):
            reason = "metadata-changed"
        elif any(owner_of(sm, p)[0] != "service" for p in changed):
            reason = "cross-owner-dependencies"
        elif any(
            entry.resolved_facts is None
            for entry in previous.files.values()
            if entry.facts.language in {"python", "java", "go"}
        ):
            reason = "missing-resolved-facts"
    dirty_paths: set[str] | None = None
    if reason is None and previous:
        affected = {owner_of(sm, p)[1] for p in changed}
        dirty_paths = changed | {p for p in previous.files if owner_of(sm, p)[1] in affected}
    capture: dict[str, Any] = {}
    timings = timings or IndexTimings()
    snap, sm = index_repo(
        root,
        commit=commit,
        timings=timings,
        progress=(
            lambda phase, done, total: progress(
                "assembled" if phase == "done" else phase, done, total
            )
        )
        if progress
        else None,
        cancelled=cancelled,
        reuse=previous.files if previous and reason is None else None,
        dirty_paths=dirty_paths,
        capture=capture,
        previous=previous.snapshot if previous and reason is None else None,
    )
    _check_cancelled(cancelled)
    if (
        service_map_fingerprint(root) != fingerprint
        or checkout_dirty(root) != dirty
        or checkout_commit(root) != head
    ):
        raise RuntimeError("checkout/configuration changed during indexing; retry")
    repo = snap.nodes[repo_node_id(sm.repo)]
    repo.props["index_generation"] = uuid.uuid4().hex
    files: dict[str, FileState] = {}
    raw: dict[str, FileFacts] = capture["raw"]
    resolved: dict[str, FileFacts] = capture["resolved"]
    # Resolution invalidates a whole affected owner above. Store that bounded
    # scope explicitly rather than copying every sibling path into every file
    # (quadratic memory and disk use for large services).
    source_nodes = {n.file: n for n in snap.by_kind(NodeKind.SOURCE_FILE)}
    node_ids: dict[str, set[str]] = {}
    edge_ids: dict[str, set[str]] = {}
    for node in snap.nodes.values():
        if node.file:
            node_ids.setdefault(node.file, set()).add(node.id)
    for edge in snap.edges:
        if edge.evidence.file:
            edge_ids.setdefault(edge.evidence.file, set()).add(edge_key(edge))
    for path in capture["files"]:
        facts = raw.get(path, FileFacts(path=path, language="metadata"))
        old = previous.files.get(path) if previous else None
        if path in source_nodes:
            content_hash = source_nodes[path].props["sha256"]
        elif old and path not in changed and reason is None:
            content_hash = old.content_hash
        else:
            content_hash = hashlib.sha256((root / path).read_bytes()).hexdigest()
        fact_key = old.facts_cache_key if old and path not in changed and reason is None else ""
        if not fact_key and Path(path).suffix in PARSER_VERSIONS:
            fact_key = cache_key(
                Path(path).suffix, PARSER_VERSIONS[Path(path).suffix], (root / path).read_bytes()
            )
        processed = resolved.get(path)
        names = {call.called for call in facts.calls}
        link_keys = {
            key
            for call in (processed.http_calls if processed else [])
            for key in (
                "operation:" + call.call_name,
                "path:" + call.method + ":" + call.url,
                "client:" + call.client_owner + ":" + call.client_id,
            )
        }
        files[path] = FileState(
            path,
            content_hash,
            fact_key,
            exported_hash(facts),
            facts,
            owner_scope=":".join(owner_of(sm, path)),
            lookup_misses=names,
            fallback_names={name.rsplit(".", 1)[-1] for name in names},
            outbound_link_keys=link_keys,
            node_ids=node_ids.get(path, set()),
            edge_ids=edge_ids.get(path, set()),
            resolved_facts=processed,
        )
    golden = root / "axon.golden.yaml"
    if golden.is_file():
        from axon.v2.golden import verify_golden

        verification = verify_golden(snap, root, golden)
        if not verification["ok"]:
            raise ValueError("golden verification failed: " + json.dumps(verification["cases"]))
    state = IndexState(sm.repo, commit, fingerprint, parser_version, files, snap)
    # Relink all outbound facts, so additions that make a path/operation ambiguous
    # invalidate unchanged callers as well. The graph delta writes changed rows only.
    combined = replace_repositories(current, snap)
    delta = GraphDelta.between(current, combined)
    apply_delta = getattr(store, "apply_delta", None)
    if progress:
        progress("publish", 0, 1)
    if hasattr(store, "publish_progress"):
        store.publish_progress = (
            (lambda done, total: progress("publish", done, total)) if progress else None
        )
    if previous is not None and apply_delta is not None and reason is None:
        try:
            apply_delta(delta)
        except DeltaNeedsFullSave:
            store.save(snap)
    else:
        store.save(snap)
    if hasattr(store, "publish_progress"):
        store.publish_progress = None
    if progress:
        progress("publish", 1, 1)
        progress("state", 0, len(files))
    state_started = time.monotonic()
    state_persisted = True
    warnings: list[str] = []
    try:
        state_store.save(state)
    except (OSError, ValueError, sqlite3.Error, OverflowError) as exc:
        state_persisted = False
        warnings.append(f"incremental state not saved: {exc}")
        try:
            state_store.path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove stale incremental state", exc_info=True)
        logger.warning("index state not persisted; next update rebuilds fully: %s", exc)
    if progress:
        progress("state", len(files), len(files))
        progress("done", 1, 1)
    return (
        snap,
        sm,
        {
            "mode": "incremental" if reason is None else "full",
            "fallback_reason": reason,
            "dirty_files": len(dirty_paths) if dirty_paths is not None else len(raw),
            "files_reused": timings.files_cached if reason is None else 0,
            "state_persisted": state_persisted,
            "state_save_s": time.monotonic() - state_started,
            "warnings": warnings,
            "store_save_s": getattr(store, "last_save_s", 0.0),
            "delta_nodes": len(delta.upsert_nodes) + len(delta.remove_node_ids),
            "delta_edges": len(delta.add_edges) + len(delta.remove_edge_keys),
        },
    )


def audit_state(
    root: Path, state_dir: str | Path | None = None, *, snapshot: GraphSnapshot | None = None
) -> dict[str, Any]:
    """Shadow full rebuild comparison; never publishes or changes persisted state."""
    sm = load_service_map(root)
    state = StateStore(sm.repo, state_dir).load(snapshot)
    if state is None:
        return {"ok": False, "reason": "missing-state"}
    if checkout_dirty(root) is not False or checkout_commit(root) != state.commit:
        return {"ok": False, "reason": "checkout-must-match-stored-clean-commit"}
    if snapshot is None:
        return {"ok": False, "reason": "graph-snapshot-required"}
    stored_repo = state.snapshot.nodes.get(repo_node_id(sm.repo))
    if stored_repo != snapshot.nodes.get(repo_node_id(sm.repo)):
        return {"ok": False, "reason": "graph-state-mismatch"}
    timings = IndexTimings()
    full, _ = index_repo(root, commit=state.commit, use_cache=False, timings=timings)
    before = state.snapshot.model_copy(deep=True)
    for snapshot in (before, full):
        for node in snapshot.by_kind(NodeKind.REPO):
            node.props.pop("index_generation", None)
    delta = GraphDelta.between(before, full)
    mismatch = bool(
        delta.upsert_nodes
        or delta.remove_node_ids
        or delta.add_edges
        or delta.remove_edge_keys
        or sorted(before.unresolved) != sorted(full.unresolved)
        or sorted(before.diagnostics) != sorted(full.diagnostics)
    )
    return {
        "ok": not mismatch,
        "commit": state.commit,
        "node_changes": len(delta.upsert_nodes) + len(delta.remove_node_ids),
        "edge_changes": len(delta.add_edges) + len(delta.remove_edge_keys),
        "full_rebuild_s": timings.total_s,
    }
