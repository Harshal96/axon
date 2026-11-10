"""Read-only upgrade preflight and graph comparison helpers.

Nothing in this module writes: no schema creation, no state save, no publish.
"""

from __future__ import annotations

import json
import sqlite3
from importlib import metadata
from pathlib import Path
from typing import Any

from axon.v1.fingerprints import build_versions
from axon.v1.graph import GraphSnapshot
from axon.v1.indexstate import SCHEMA_VERSION, StateStore
from axon.v1.models import GRAPH_SCHEMA_VERSION, EdgeKind, GraphSchemaError
from axon.v1.store import load_snapshot_file


def package_version() -> str:
    for name in ("axon", "axon-core", "axon-cli"):
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return "unknown"


def inspect_snapshot(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        return {"path": str(target), "status": "missing"}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"path": str(target), "status": "unreadable", "error": str(exc)}
    version = raw.get("schema_version", 1) if isinstance(raw, dict) else 1
    try:
        snap = load_snapshot_file(target)
    except GraphSchemaError as exc:
        return {
            "path": str(target),
            "status": "too-new" if int(version or 0) > GRAPH_SCHEMA_VERSION else "unreadable",
            "schema_version": version,
            "supported_schema_version": GRAPH_SCHEMA_VERSION,
            "error": str(exc),
        }
    return {
        "path": str(target),
        "status": "compatible",
        "schema_version": snap.schema_version,
        "supported_schema_version": GRAPH_SCHEMA_VERSION,
        "nodes": len(snap.nodes),
        "edges": len(snap.edges),
    }


def inspect_service_map(repo: str | Path) -> dict[str, Any]:
    from axon.v1.servicemap import load_service_map

    try:
        sm = load_service_map(Path(repo))
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    return {"status": "compatible", "version": sm.version, "repo": sm.repo}


def inspect_state(repo_id: str, state_dir: str | Path | None = None) -> dict[str, Any]:
    store = StateStore(repo_id, state_dir)
    if not store.path.is_file():
        return {"path": str(store.path), "status": "missing"}
    try:
        with sqlite3.connect(f"{store.path.as_uri()}?mode=ro", uri=True) as db:
            row = db.execute("SELECT version, commit_sha, parser_version FROM meta").fetchone()
    except sqlite3.Error as exc:
        return {"path": str(store.path), "status": "unreadable", "error": str(exc)}
    if row is None:
        return {"path": str(store.path), "status": "missing"}
    version, commit, parser_version = row
    rebuild = version != SCHEMA_VERSION or parser_version != build_versions()
    return {
        "path": str(store.path),
        "status": "rebuild-needed" if rebuild else "compatible",
        "schema_version": version,
        "supported_schema_version": SCHEMA_VERSION,
        "commit": commit,
    }


def inspect_neo4j() -> dict[str, Any]:
    from axon.v1.store import Neo4jStore

    store = Neo4jStore()
    try:
        pubs = store.stored_publications()
    except Exception as exc:
        return {"status": "unreachable", "error": str(exc)}
    too_new = [
        pub
        for pub in pubs
        if pub.get("schema_version") is not None
        and int(pub["schema_version"]) > GRAPH_SCHEMA_VERSION
    ]
    return {
        "status": "too-new" if too_new else "compatible",
        "supported_schema_version": GRAPH_SCHEMA_VERSION,
        "publications": pubs,
    }


def upgrade_check(
    repo: str | Path | None = None,
    snapshot: str | Path | None = None,
    *,
    neo4j: bool = False,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    report: dict[str, Any] = {
        "axon_version": package_version(),
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "checks": checks,
    }
    if snapshot is not None:
        checks["snapshot"] = inspect_snapshot(snapshot)
    if repo is not None:
        checks["service_map"] = inspect_service_map(repo)
        if checks["service_map"].get("repo"):
            checks["state"] = inspect_state(str(checks["service_map"]["repo"]), state_dir)
    if neo4j:
        checks["neo4j"] = inspect_neo4j()
    statuses = {str(c.get("status")) for c in checks.values() if isinstance(c, dict)}
    if "too-new" in statuses or "unreadable" in statuses or "error" in statuses:
        report["verdict"] = "blocked"
    elif "rebuild-needed" in statuses:
        report["verdict"] = "rebuild-needed"
    elif checks:
        report["verdict"] = "compatible"
    else:
        report["verdict"] = "nothing-checked"
    return report


def compare_snapshots(before: GraphSnapshot, after: GraphSnapshot) -> dict[str, Any]:
    def counts(snap: GraphSnapshot) -> dict[str, int]:
        out: dict[str, int] = {}
        for node in snap.nodes.values():
            out[node.kind.value] = out.get(node.kind.value, 0) + 1
        return out

    def outbound(snap: GraphSnapshot) -> set[str]:
        return {
            f"{e.from_id}|{e.to_id}|{e.evidence.rule}"
            for e in snap.edges
            if e.kind == EdgeKind.CALLS_API
        }

    old, new = outbound(before), outbound(after)
    before_eps = {n.id for n in before.nodes.values() if n.kind.value == "ApiEndpoint"}
    after_eps = {n.id for n in after.nodes.values() if n.kind.value == "ApiEndpoint"}
    return {
        "nodes_before": len(before.nodes),
        "nodes_after": len(after.nodes),
        "edges_before": len(before.edges),
        "edges_after": len(after.edges),
        "nodes_by_kind_before": counts(before),
        "nodes_by_kind_after": counts(after),
        "coverage_before": before.coverage_by_service(),
        "coverage_after": after.coverage_by_service(),
        "endpoints_removed": sorted(before_eps - after_eps),
        "endpoints_added": sorted(after_eps - before_eps),
        "outbound_added": sorted(new - old),
        "outbound_removed": sorted(old - new),
        "unresolved_before": before.unresolved,
        "unresolved_after": after.unresolved,
    }
