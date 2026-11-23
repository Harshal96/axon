"""Conservative incremental resolution with transactional graph/state publication.

Service roots are an isolation boundary. Until shared receiver dependencies can
be proven individually, repositories with shared/unowned/client source fall back
to full resolution. Endpoint linking always runs against the new complete set.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from axon.v1.facts import FileFacts
from axon.v1.factscache import cache_key
from axon.v1.golden import validate_golden
from axon.v1.graph import GraphSnapshot
from axon.v1.graph_delta import GraphDelta, edge_key, lost_cross_repo_links
from axon.v1.indexer import PARSER_VERSIONS, IndexTimings, _check_cancelled, index_repo
from axon.v1.indexstate import FileState, IndexState, StateStore, fallback_reason, git_changes
from axon.v1.indexstate import interface_hash as exported_hash
from axon.v1.models import EdgeKind, NodeKind, repo_node_id
from axon.v1.revision import checkout_commit, checkout_dirty, resolve_index_commit
from axon.v1.servicemap import ServiceMap, load_service_map, owner_of, service_map_fingerprint
from axon.v1.store import GraphStore

logger = logging.getLogger(__name__)
_LOCKS_GUARD = threading.Lock()
_REPO_LOCKS: dict[str, threading.Lock] = {}


class SourcePathConflict(ValueError):
    """The repository ID and checkout path conflict with the published mapping."""


class CheckoutChanged(RuntimeError):
    """The checkout changed before its indexed data could be published."""


def check_source_identity(
    snap: GraphSnapshot, repo: str, root: Path, *, replace_source: bool = False
) -> None:
    path = root.resolve()
    node = snap.nodes.get(repo_node_id(repo))
    source = node.props.get("source_path", "") if node else ""
    if source and Path(source).resolve() != path and not replace_source:
        raise SourcePathConflict(
            f"repository {repo!r} already uses source path {source!r}; "
            "set replace_source=true to reassign it"
        )
    for other in snap.by_kind(NodeKind.REPO):
        other_path = other.props.get("source_path", "")
        if other.repo_id != repo and other_path and Path(other_path).resolve() == path:
            raise SourcePathConflict(
                f"checkout {str(path)!r} is already published as repository {other.repo_id!r}; "
                f"remove repository {other.repo_id!r} from Axon before indexing it as {repo!r}"
            )


def publication_result(snap: GraphSnapshot) -> dict[str, Any]:
    stats = snap.publication_stats
    missing = sorted(
        repo
        for repo, report in snap.link_reports.items()
        if any("link-inputs-missing" in line for line in report.diagnostics)
    )
    return {
        "publication_id": snap.publication_id,
        "link_sites_replayed": stats.replayed_sites
        or sum(len(inputs.prepared) + len(inputs.pinned) for inputs in snap.link_inputs.values()),
        "link_reconcile_s": stats.reconcile_s,
        "link_rows_staged": stats.staged_rows,
        "relinked_repos": sorted(snap.link_reports),
        "link_inputs_missing": missing,
    }


def index_and_publish(
    root: Path, store: GraphStore, *, expected_repo: str = "", **kwargs: Any
) -> tuple[GraphSnapshot, ServiceMap, dict[str, Any]]:
    # Graph and state identities use the declared repository id, even when two
    # server checkouts have different physical paths.
    identity = load_service_map(root).repo
    if expected_repo and identity != expected_repo:
        raise CheckoutChanged("repository ID changed before indexing; submit a new index request")
    with _LOCKS_GUARD:
        lock = _REPO_LOCKS.setdefault(identity, threading.Lock())
    with lock:
        return _index_and_publish(root, store, identity=identity, **kwargs)


def _index_and_publish(
    root: Path,
    store: GraphStore,
    *,
    identity: str,
    commit: str = "",
    base_commit: str = "",
    force: bool = False,
    timings: IndexTimings | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    cancelled: threading.Event | None = None,
    state_dir: str | Path | None = None,
    services: list[str] | None = None,
    api: str = "",
    use_cache: bool | None = None,
    replace_source: bool = False,
) -> tuple[GraphSnapshot, ServiceMap, dict[str, Any]]:
    root = root.resolve()
    sm = load_service_map(root)
    if sm.repo != identity:
        raise CheckoutChanged("repository ID changed before indexing; submit a new index request")
    head = checkout_commit(root)
    commit = resolve_index_commit(root, commit, head=head)
    dirty = checkout_dirty(root)
    from axon.v1.scoped_index import (
        endpoint_scoped_paths,
        evict_scoped_facts,
        focused_api_result,
        focused_result,
        repository_view,
        resolve_endpoint,
        scoped_paths,
    )

    fresh_paths = scoped_paths(root, sm, services or [])
    evict_scoped_facts(root, fresh_paths)
    fingerprint = service_map_fingerprint(root)

    def check_checkout() -> None:
        if (
            service_map_fingerprint(root) != fingerprint
            or checkout_dirty(root) != dirty
            or checkout_commit(root) != head
        ):
            raise CheckoutChanged("checkout/configuration changed during indexing; retry")

    from axon.v1.fingerprints import build_versions

    parser_version = build_versions()
    state_store = StateStore(sm.repo, state_dir)
    current = store.load()
    check_source_identity(current, sm.repo, root, replace_source=replace_source)
    api_endpoint_id = ""
    if api:
        api_endpoint_id = resolve_endpoint(
            current, api, services[0] if services else "", repo=sm.repo
        ).id
        fresh_paths = endpoint_scoped_paths(root, sm, current, api_endpoint_id)
        evict_scoped_facts(root, fresh_paths)
    previous = state_store.load(current)
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
    changed.update(fresh_paths)
    dirty_paths: set[str] | None = None
    if reason is None and previous:
        affected = {owner_of(sm, p)[1] for p in changed}
        dirty_paths = changed | {p for p in previous.files if owner_of(sm, p)[1] in affected}
    capture: dict[str, Any] = {}
    timings = timings or IndexTimings()
    snap, sm = index_repo(
        root,
        force_parse_paths=fresh_paths,
        use_cache=use_cache,
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
    if sm.repo != identity:
        raise CheckoutChanged("repository ID changed during indexing; submit a new index request")
    check_checkout()
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
    link_losses: list[dict[str, str | int]] = []

    def validate(candidate: GraphSnapshot) -> None:
        _check_cancelled(cancelled)
        # Stores retain the outgoing immutable snapshot until commit. Read it
        # under their publication lock: candidate already replaced its Repo node,
        # and the pre-parse identity check can race with another publisher.
        before = store.load()
        check_source_identity(before, sm.repo, root, replace_source=replace_source)
        validate_golden(candidate, root)
        link_losses.extend(lost_cross_repo_links(before, candidate, {sm.repo}))
        check_checkout()
        _check_cancelled(cancelled)

    state = IndexState(sm.repo, commit, fingerprint, parser_version, files, snap)
    if progress:
        progress("publish", 0, 1)

    def publish_progress(done: int, total: int) -> None:
        _check_cancelled(cancelled)
        if progress:
            progress("publish", done, total)
        if done == total - 1:
            # The persistent store reserves the final step for activating its marker.
            check_checkout()
        _check_cancelled(cancelled)

    started = time.monotonic()
    if hasattr(store, "publish_progress"):
        store.publish_progress = publish_progress
    try:
        committed = store.publish_repository(snap, repo_id=sm.repo, validate=validate)
    finally:
        if hasattr(store, "publish_progress"):
            store.publish_progress = None
    publish_elapsed = time.monotonic() - started
    publication = publication_result(committed)
    if not committed.publication_stats.total_s:
        publication["link_reconcile_s"] = publish_elapsed
    delta = GraphDelta.between(current, committed)
    # A returned repository view must retain its remote targets and exact report,
    # without sharing mutable node/input containers with any published reader.
    local_link_diagnostics = {
        line for report in snap.link_reports.values() for line in report.diagnostics
    }
    structural_diagnostics = [
        line for line in snap.diagnostics if line not in local_link_diagnostics
    ]
    snap = repository_view(committed, sm.repo)
    snap.diagnostics = list(dict.fromkeys(structural_diagnostics + snap.diagnostics))
    warnings: list[str] = []

    def committed_progress(phase: str, done: int, total: int) -> None:
        if progress:
            try:
                progress(phase, done, total)
            except Exception as exc:
                # Observer failures cannot turn an already committed graph into
                # a cancelled/failed index result, including IndexCancelled.
                warning = (
                    f"progress callback failed after commit ({phase}): {type(exc).__name__}: {exc}"
                )
                warnings.append(warning)
                logger.warning(warning)

    committed_progress("publish", 1, 1)
    committed_progress("state", 0, len(files))
    state_started = time.monotonic()
    state_persisted = True
    try:
        state_store.save(state)
    except Exception as exc:
        # State is advisory; even an unexpected backend failure cannot undo commit.
        state_persisted = False
        warnings.append(f"incremental state not saved: {exc}")
        try:
            state_store.path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove stale incremental state", exc_info=True)
        logger.warning("index state not persisted; next update rebuilds fully: %s", exc)
    committed_progress("state", len(files), len(files))
    committed_progress("done", 1, 1)
    return (
        snap,
        sm,
        {
            **(
                focused_api_result(current, committed, api_endpoint_id)
                if api_endpoint_id
                else focused_result(current, committed, sm.repo, services)
                if services
                else {}
            ),
            **publication,
            "lost_cross_repo_links": link_losses[:50],
            "lost_cross_repo_link_count": len(link_losses),
            "mode": "incremental" if reason is None else "full",
            "fallback_reason": reason,
            "dirty_files": len(dirty_paths) if dirty_paths is not None else len(raw),
            "files_reused": timings.files_cached if reason is None else 0,
            "state_persisted": state_persisted,
            "state_save_s": time.monotonic() - state_started,
            "warnings": warnings,
            "store_save_s": committed.publication_stats.total_s or publish_elapsed,
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
    # Audit source structure only. Shared-catalog link projections can change
    # without a source generation change or any access to the caller checkout.
    for snapshot in (before, full):
        snapshot.nodes = {
            key: node
            for key, node in snapshot.nodes.items()
            if node.repo_id == sm.repo and node.kind != NodeKind.EXTERNAL_API
        }
        snapshot.edges = [
            edge
            for edge in snapshot.edges
            if edge.kind != EdgeKind.CALLS_API
            and edge.from_id in snapshot.nodes
            and edge.to_id in snapshot.nodes
        ]
        for node in snapshot.by_kind(NodeKind.REPO):
            node.props.pop("index_generation", None)
    delta = GraphDelta.between(before, full)
    mismatch = bool(
        delta.upsert_nodes or delta.remove_node_ids or delta.add_edges or delta.remove_edge_keys
    )
    return {
        "ok": not mismatch,
        "commit": state.commit,
        "node_changes": len(delta.upsert_nodes) + len(delta.remove_node_ids),
        "edge_changes": len(delta.add_edges) + len(delta.remove_edge_keys),
        "full_rebuild_s": timings.total_s,
    }
