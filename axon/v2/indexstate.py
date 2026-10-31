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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axon.v2.facts import FileFacts, facts_from_dict, facts_to_dict
from axon.v2.graph import GraphSnapshot

SCHEMA_VERSION = 1


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
        return "parser-changed"
    if changed_files > max(total_files, 1) * 0.15:
        return "change-threshold"
    return None


class StateStore:
    """One atomic SQLite record per repository; malformed records are cache misses."""

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

    def load(self) -> IndexState | None:
        if not self.path.is_file():
            return None
        try:
            with sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True) as connection:
                row = connection.execute(
                    "SELECT version, payload FROM state WHERE id = 1"
                ).fetchone()
            if row is None or row[0] != SCHEMA_VERSION:
                return None
            data = json.loads(row[1])
            if data["repo_id"] != self.repo_id:
                return None
            files = {}
            for path, raw in data["files"].items():
                facts = facts_from_dict(raw.pop("facts"))
                resolved = raw.pop("resolved_facts", None)
                resolved_facts = facts_from_dict(resolved) if resolved is not None else None
                for key in (
                    "dependencies",
                    "lookup_misses",
                    "fallback_names",
                    "outbound_link_keys",
                    "node_ids",
                    "edge_ids",
                ):
                    raw[key] = set(raw[key])
                files[path] = FileState(facts=facts, resolved_facts=resolved_facts, **raw)
                if (
                    path != files[path].path
                    or facts.path != path
                    or (resolved_facts is not None and resolved_facts.path != path)
                ):
                    return None
            return IndexState(
                repo_id=data["repo_id"],
                commit=data["commit"],
                config_fingerprint=data["config_fingerprint"],
                parser_version=data["parser_version"],
                files=files,
                snapshot=GraphSnapshot.model_validate(data["snapshot"]),
            )
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def save(self, state: IndexState) -> None:
        """Atomically replace state. Caller must first successfully save the graph."""
        if state.repo_id != self.repo_id:
            raise ValueError("state repository mismatch")
        files: dict[str, Any] = {}
        for path, entry in state.files.items():
            if path != entry.path or path != entry.facts.path:
                raise ValueError("state file path mismatch")
            files[path] = {
                "path": path,
                "content_hash": entry.content_hash,
                "facts_cache_key": entry.facts_cache_key,
                "interface_hash": entry.interface_hash,
                "facts": facts_to_dict(entry.facts),
                "resolved_facts": facts_to_dict(entry.resolved_facts)
                if entry.resolved_facts is not None
                else None,
                **{
                    key: sorted(getattr(entry, key))
                    for key in (
                        "dependencies",
                        "lookup_misses",
                        "fallback_names",
                        "outbound_link_keys",
                        "node_ids",
                        "edge_ids",
                    )
                },
            }
        payload = json.dumps(
            {
                "repo_id": state.repo_id,
                "commit": state.commit,
                "config_fingerprint": state.config_fingerprint,
                "parser_version": state.parser_version,
                "files": files,
                "snapshot": state.snapshot.model_dump(mode="json"),
            }
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix="state-", suffix=".db", dir=self.path.parent
        )
        os.close(descriptor)
        try:
            with sqlite3.connect(temporary) as connection:
                connection.execute(
                    "CREATE TABLE state (id INTEGER PRIMARY KEY CHECK (id = 1), "
                    "version INTEGER NOT NULL, payload TEXT NOT NULL)"
                )
                connection.execute("INSERT INTO state VALUES (1, ?, ?)", (SCHEMA_VERSION, payload))
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def save_after_graph(
        self, state: IndexState, save_graph: Callable[[GraphSnapshot], None]
    ) -> None:
        save_graph(state.snapshot)
        self.save(state)
