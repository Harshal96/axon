"""Persistent incremental-index evidence and conservative invalidation helpers.

This module does not execute incremental resolution. State is advisory: absent or
invalid state must cause a full index. Publish it only after graph persistence.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axon.v2.facts import FileFacts, facts_from_dict, facts_to_dict
from axon.v2.graph import GraphSnapshot
from axon.v2.models import GraphNode, repo_node_id

SCHEMA_VERSION = 2


@dataclass
class FileState:
    path: str
    content_hash: str
    facts_cache_key: str
    interface_hash: str
    facts: FileFacts
    dependencies: set[str] = field(default_factory=set)
    lookup_misses: set[str] = field(default_factory=set)
    fallback_names: set[str] = field(default_factory=set)
    outbound_link_keys: set[str] = field(default_factory=set)
    node_ids: set[str] = field(default_factory=set)
    edge_ids: set[str] = field(default_factory=set)
    resolved_facts: FileFacts | None = None
    owner_scope: str = ""


@dataclass
class IndexState:
    repo_id: str
    commit: str
    config_fingerprint: str
    parser_version: str
    files: dict[str, FileState]
    snapshot: GraphSnapshot


def interface_hash(facts: FileFacts) -> str:
    """Hash source-visible exports, excluding calls, body extents and locations."""
    data = facts_to_dict(facts)
    for key in ("calls", "http_calls", "diagnostics", "diagnostics_truncated", "suppressed_calls"):
        data.pop(key, None)
    # Local assignments are implementation details; class/module bindings export evidence.
    classes = {s.qualified for s in facts.symbols if s.kind == "CLASS"}
    data["bindings"] = [b for b in data["bindings"] if not b["caller"] or b["caller"] in classes]

    def without_locations(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: without_locations(v)
                for k, v in value.items()
                if k not in {"line", "line_start", "line_end"}
            }
        if isinstance(value, list):
            return [without_locations(v) for v in value]
        return value

    return hashlib.sha256(
        json.dumps(without_locations(data), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def symbols_changed(before: FileFacts | None, after: FileFacts | None) -> set[str]:
    """Conservative lookup keys exported by either side of an interface change."""
    if before is not None and after is not None and interface_hash(before) == interface_hash(after):
        return set()
    keys: set[str] = set()
    for facts in (before, after):
        if facts is None:
            continue
        module = str(Path(facts.path).with_suffix("")).replace("/", ".")
        if module.endswith(".__init__"):
            module = module.removesuffix(".__init__")
        keys.update((module, module.rsplit(".", 1)[-1]))
        for symbol in facts.symbols:
            keys.update((symbol.qualified, symbol.name, f"{module}.{symbol.qualified}"))
        for name in set(facts.constants) | set(facts.returns) | set(facts.returns_inferred):
            keys.update((name, name.rsplit(".", 1)[-1], f"{module}.{name}"))
        for binding in facts.bindings:
            keys.add(binding.var)
    return keys


def expand_dirty_set(
    changed: Iterable[str], files: Mapping[str, FileState], changed_names: Iterable[str] = ()
) -> set[str]:
    """Transitively invalidate readers and negative/fallback name lookups.

    Callers must supply newly introduced and removed lookup names; a failed
    lookup has no dependency path and cannot otherwise be invalidated soundly.
    """
    dirty = set(changed)
    names = set(changed_names)
    while True:
        readers = {
            path
            for path, state in files.items()
            if state.dependencies & dirty or (state.lookup_misses | state.fallback_names) & names
        }
        if readers <= dirty:
            return dirty
        dirty.update(readers)


@dataclass(frozen=True)
class FileChange:
    status: str
    path: str
    old_path: str | None = None


def parse_name_status(data: bytes) -> list[FileChange]:
    """Parse git's NUL-delimited status, including both paths of renames/copies."""
    tokens = data.split(b"\0")
    if tokens[-1] != b"":
        raise ValueError("unterminated git diff")
    tokens.pop()
    result: list[FileChange] = []
    cursor = 0
    while cursor < len(tokens):
        status = tokens[cursor].decode("ascii")
        cursor += 1
        if (
            not status
            or status[0] not in "ACDMRTUXB"
            or (len(status) > 1 and (status[0] not in "RCM" or not status[1:].isdigit()))
        ):
            raise ValueError("invalid git status")
        count = 2 if status[0] in "RC" else 1
        if cursor + count > len(tokens) or any(not p for p in tokens[cursor : cursor + count]):
            raise ValueError("missing git path")
        paths = [os.fsdecode(p) for p in tokens[cursor : cursor + count]]
        cursor += count
        result.append(FileChange(status, paths[-1], paths[0] if count == 2 else None))
    return result


def git_changes(root: Path, before: str, after: str) -> list[FileChange] | None:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "diff",
                "--name-status",
                "-z",
                "--find-renames",
                before,
                after,
                "--",
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return parse_name_status(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
        return None


def fallback_reason(
    previous: IndexState | None,
    config_fingerprint: str,
    parser_version: str,
    changed_files: int,
    total_files: int,
) -> str | None:
    if previous is None:
        return "missing-state"
    if previous.config_fingerprint != config_fingerprint:
        return "config-changed"
    if previous.parser_version != parser_version:
        try:
            before, after = json.loads(previous.parser_version), json.loads(parser_version)
            if before.get("indexer") != after.get("indexer") or before.get("state") != after.get(
                "state"
            ):
                return "indexer-changed"
        except (ValueError, AttributeError):
            pass
        return "parser-changed"
    if changed_files > max(total_files, 1) * 0.15:
        return "change-threshold"
    return None


class StateStore:
    """Normalized per-file SQLite state; malformed or older schemas are cache misses."""

    def __init__(self, repo_id: str, state_dir: str | Path | None = None) -> None:
        self.repo_id = repo_id
        root = Path(
            state_dir or os.environ.get("AXON_STATE_DIR", str(Path.home() / ".axon" / "state"))
        )
        # Retain ordinary IDs while preventing path traversal or directory collisions.
        directory = (
            repo_id
            if repo_id not in {"", ".", ".."} and all(c.isalnum() or c in "._-" for c in repo_id)
            else hashlib.sha256(repo_id.encode()).hexdigest()
        )
        self.path = root / directory / "state.db"

    def load(self, snapshot: GraphSnapshot | None = None) -> IndexState | None:
        """Load advisory facts; graph rows come from the already published store.

        Without a snapshot, only repository-level metadata is available. This is
        useful for inspecting state, but callers must supply the store snapshot
        before reusing facts or auditing the graph.
        """
        if not self.path.is_file():
            return None
        try:
            with sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                meta = db.execute("SELECT * FROM meta").fetchone()
                if (
                    meta is None
                    or meta["version"] != SCHEMA_VERSION
                    or meta["repo_id"] != self.repo_id
                ):
                    return None
                files = {}
                for row in db.execute("SELECT * FROM files"):
                    path = row["path"]
                    facts = facts_from_dict(json.loads(zlib.decompress(row["facts"])))
                    resolved = row["resolved_facts"]
                    resolved_facts = (
                        facts_from_dict(json.loads(zlib.decompress(resolved)))
                        if resolved is not None
                        else None
                    )
                    if facts.path != path or (
                        resolved_facts is not None and resolved_facts.path != path
                    ):
                        return None
                    files[path] = FileState(
                        path,
                        row["content_hash"],
                        row["facts_cache_key"],
                        row["interface_hash"],
                        facts,
                        dependencies=set(json.loads(row["deps"])),
                        lookup_misses=set(json.loads(row["lookup_misses"])),
                        fallback_names=set(json.loads(row["fallback_names"])),
                        outbound_link_keys=set(json.loads(row["outbound_link_keys"])),
                        resolved_facts=resolved_facts,
                        owner_scope=row["owner_scope"],
                    )
                for row in db.execute("SELECT path, node_id FROM file_nodes"):
                    files[row[0]].node_ids.add(row[1])
                for row in db.execute("SELECT path, edge_key FROM file_edges"):
                    files[row[0]].edge_ids.add(row[1])
                repo_nodes = {
                    row[0]: GraphNode.model_validate_json(row[1])
                    for row in db.execute("SELECT node_id, payload FROM repo_nodes")
                }
                # Keep the persisted Repo marker, so the existing mismatch check
                # compares its generation with the currently published graph.
                if snapshot is not None:
                    ids = {n.id for n in snapshot.nodes.values() if n.repo_id == self.repo_id}
                    ids.update(node_id for node_id in repo_nodes if node_id in snapshot.nodes)
                    ids.update(
                        node_id
                        for entry in files.values()
                        for node_id in entry.node_ids
                        if node_id in snapshot.nodes
                    )
                    nodes = {node_id: snapshot.nodes[node_id] for node_id in ids}
                    repo_marker = repo_nodes.get(repo_node_id(self.repo_id))
                    if repo_marker is not None:
                        nodes[repo_marker.id] = repo_marker
                    graph = GraphSnapshot(
                        nodes=nodes,
                        edges=[e for e in snapshot.edges if e.from_id in ids and e.to_id in ids],
                    )
                else:
                    graph = GraphSnapshot(nodes=repo_nodes)
                graph.commit = meta["commit_sha"]
                graph.unresolved = json.loads(meta["unresolved"])
                graph.diagnostics = json.loads(meta["diagnostics"])
                return IndexState(
                    self.repo_id,
                    meta["commit_sha"],
                    meta["config_fingerprint"],
                    meta["parser_version"],
                    files,
                    graph,
                )
        except (
            sqlite3.Error,
            OSError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            AttributeError,
            zlib.error,
        ):
            return None

    def save(self, state: IndexState) -> None:
        """Stream compressed per-file rows into a temporary database, then rename.

        Never serialize the graph snapshot or all file facts in a single value.
        Each file is independently compressed and released before the next row.
        """
        if state.repo_id != self.repo_id:
            raise ValueError("state repository mismatch")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix="state-", suffix=".db", dir=self.path.parent
        )
        os.close(descriptor)
        try:
            with sqlite3.connect(temporary) as db:
                db.executescript("""
                    CREATE TABLE meta (version INTEGER, repo_id TEXT, commit_sha TEXT,
                        config_fingerprint TEXT, parser_version TEXT, generation TEXT,
                        unresolved TEXT, diagnostics TEXT);
                    CREATE TABLE files (path TEXT PRIMARY KEY, content_hash TEXT,
                        facts_cache_key TEXT, interface_hash TEXT, facts BLOB,
                        resolved_facts BLOB, deps TEXT, lookup_misses TEXT,
                        fallback_names TEXT, outbound_link_keys TEXT, owner_scope TEXT);
                    CREATE TABLE file_nodes (path TEXT, node_id TEXT, PRIMARY KEY(path, node_id));
                    CREATE TABLE file_edges (path TEXT, edge_key TEXT, PRIMARY KEY(path, edge_key));
                    CREATE TABLE repo_nodes (node_id TEXT PRIMARY KEY, payload TEXT);
                """)
                repo = state.snapshot.nodes.get(repo_node_id(self.repo_id))
                db.execute(
                    "INSERT INTO meta VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        SCHEMA_VERSION,
                        self.repo_id,
                        state.commit,
                        state.config_fingerprint,
                        state.parser_version,
                        repo.props.get("index_generation", "") if repo else "",
                        json.dumps(state.snapshot.unresolved),
                        json.dumps(state.snapshot.diagnostics),
                    ),
                )
                for path, entry in state.files.items():
                    if (
                        path != entry.path
                        or path != entry.facts.path
                        or (entry.resolved_facts is not None and entry.resolved_facts.path != path)
                    ):
                        raise ValueError("state file path mismatch")

                    def pack(facts: FileFacts) -> bytes:
                        return zlib.compress(
                            json.dumps(facts_to_dict(facts), separators=(",", ":")).encode()
                        )

                    db.execute(
                        "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            path,
                            entry.content_hash,
                            entry.facts_cache_key,
                            entry.interface_hash,
                            pack(entry.facts),
                            pack(entry.resolved_facts)
                            if entry.resolved_facts is not None
                            else None,
                            *(
                                json.dumps(sorted(getattr(entry, key)))
                                for key in (
                                    "dependencies",
                                    "lookup_misses",
                                    "fallback_names",
                                    "outbound_link_keys",
                                )
                            ),
                            entry.owner_scope,
                        ),
                    )
                    db.executemany(
                        "INSERT INTO file_nodes VALUES (?, ?)",
                        ((path, value) for value in entry.node_ids),
                    )
                    db.executemany(
                        "INSERT INTO file_edges VALUES (?, ?)",
                        ((path, value) for value in entry.edge_ids),
                    )
                owned = {node_id for entry in state.files.values() for node_id in entry.node_ids}
                db.executemany(
                    "INSERT INTO repo_nodes VALUES (?, ?)",
                    (
                        (node.id, node.model_dump_json())
                        for node in state.snapshot.nodes.values()
                        if node.id not in owned
                    ),
                )
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def save_after_graph(
        self, state: IndexState, save_graph: Callable[[GraphSnapshot], None]
    ) -> None:
        save_graph(state.snapshot)
        self.save(state)
