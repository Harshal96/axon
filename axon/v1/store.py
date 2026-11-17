"""Graph stores: in-memory (tests/CLI), snapshot files, and Neo4j."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from pydantic import ValidationError

from axon.v1.graph import GraphSnapshot
from axon.v1.graph_delta import GraphDelta, edge_key
from axon.v1.link_publication import (
    active_node_predicate,
    active_relationship_predicate,
    decode_link_inputs,
    decode_link_reports,
    public_properties,
    public_query_value,
)
from axon.v1.models import (
    GRAPH_SCHEMA_VERSION,
    EdgeKind,
    GraphEdge,
    GraphNode,
    GraphSchemaError,
    NodeKind,
    check_graph_schema,
)

if TYPE_CHECKING:
    from axon.v1.link_inputs import RepoLinkInputs

logger = logging.getLogger(__name__)

NEO4J_BATCH_SIZE = 5000


class QueryValidationError(ValueError):
    """Safe, actionable rejection before database execution."""


class QueryExecutionError(RuntimeError):
    """Public error containing no database message, values, or rewritten query."""


def public_query_error(exc: Exception) -> str:
    if isinstance(exc, (QueryValidationError, QueryExecutionError)):
        return str(exc)
    if isinstance(exc, GraphSchemaError):
        return "Stored graph schema is incompatible or unreadable. Upgrade Axon or re-index."
    return "Graph query execution failed; check query types, syntax, and database availability."


_T = TypeVar("_T")


def _chunks(items: list[_T], size: int) -> Iterator[list[_T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class GraphStore(Protocol):
    def save(self, snap: GraphSnapshot) -> None: ...
    def apply_delta(self, delta: GraphDelta) -> None: ...
    def publish_repository(
        self,
        replacement: GraphSnapshot,
        *,
        repo_id: str,
        validate: Callable[[GraphSnapshot], None] | None = None,
    ) -> GraphSnapshot: ...
    def remove_repository(self, repo_id: str) -> GraphSnapshot: ...
    def load(self) -> GraphSnapshot: ...
    def run_cypher(self, cypher: str) -> list[dict[str, Any]]: ...


class MemoryStore:
    def __init__(self, snap: GraphSnapshot | None = None) -> None:
        self.snap = snap or GraphSnapshot()
        self._write_lock = threading.Lock()
        self.last_save_s = 0.0

    @staticmethod
    def _repos(snap: GraphSnapshot) -> set[str]:
        return {n.repo_id for n in snap.nodes.values() if n.kind == NodeKind.REPO and n.repo_id}

    def _publish_locked(
        self,
        replacement: GraphSnapshot,
        repos: set[str],
        removed: frozenset[str] = frozenset(),
        validate: Callable[[GraphSnapshot], None] | None = None,
    ) -> GraphSnapshot:
        from axon.v1.repository_links import reconcile_repositories

        for node_id, node in replacement.nodes.items():
            previous = self.snap.nodes.get(node_id)
            if (
                (node.repo_id in repos or node.kind == NodeKind.EXTERNAL_API)
                and previous is not None
                and previous.repo_id
                and previous.repo_id not in repos
                and (node.kind == NodeKind.EXTERNAL_API or previous.repo_id not in removed)
            ):
                raise ValueError(f"node {node_id!r} is owned by another repository")
        candidate = reconcile_repositories(
            self.snap, replacement, replace_repo_ids=repos, remove_repo_ids=removed
        )
        for repo in repos:
            candidate.link_inputs[repo].source_generation = uuid.uuid4().hex
        candidate.publication_id = uuid.uuid4().hex
        if validate is not None:
            validate(candidate)
        self.snap = candidate
        return candidate

    def save(self, snap: GraphSnapshot) -> None:
        """Import explicitly owned repositories and replay the shared catalog."""
        started = time.perf_counter()
        repos = self._repos(snap)
        if not repos:
            raise ValueError("snapshot must contain an explicitly owned repository")
        with self._write_lock:
            self._publish_locked(snap, repos)
        self.last_save_s = time.perf_counter() - started

    def apply_delta(self, delta: GraphDelta) -> None:
        """Apply a structural delta and replay all callers against its catalog."""
        started = time.perf_counter()
        with self._write_lock:
            current = self.snap
            target = delta.apply(current)
            current_repos = self._repos(current)
            target_repos = self._repos(target)
            removed_repos = {
                current.nodes[node_id].repo_id
                for node_id in delta.remove_node_ids
                if node_id in current.nodes and current.nodes[node_id].kind == NodeKind.REPO
            }
            if not current_repos - target_repos <= removed_repos:
                raise ValueError("delta overwrites a repository-owned marker")
            changed = {n.repo_id for n in delta.upsert_nodes.values() if n.repo_id}
            changed.update(
                current.nodes[node_id].repo_id
                for node_id in delta.remove_node_ids
                if node_id in current.nodes and current.nodes[node_id].repo_id
            )
            for edge in delta.add_edges:
                if edge.kind != EdgeKind.CALLS_API and edge.from_id in target.nodes:
                    changed.add(target.nodes[edge.from_id].repo_id)
            for edge in current.edges:
                if edge.kind != EdgeKind.CALLS_API and edge_key(edge) in delta.remove_edge_keys:
                    if edge.from_id in current.nodes:
                        changed.add(current.nodes[edge.from_id].repo_id)
            # GraphDelta.between records which input entries changed relative to
            # its own base. Its complete metadata map may be stale for other
            # repositories, so absence or difference against the live map is
            # not evidence that those repositories were replaced.
            if delta.changed_link_input_repos is not None:
                changed.update(delta.changed_link_input_repos)
            changed.discard("")
            if not changed <= current_repos | target_repos:
                raise ValueError("delta changes nodes without an explicitly owned repository")
            self._publish_locked(
                target, changed & target_repos, frozenset(current_repos - target_repos)
            )
        self.last_save_s = time.perf_counter() - started

    def publish_repository(
        self,
        replacement: GraphSnapshot,
        *,
        repo_id: str,
        validate: Callable[[GraphSnapshot], None] | None = None,
    ) -> GraphSnapshot:
        """Replay retained callers under the same lock as the snapshot swap."""
        if repo_id not in self._repos(replacement):
            raise ValueError(f"replacement does not contain repository {repo_id!r}")
        with self._write_lock:
            return self._publish_locked(replacement, {repo_id}, validate=validate)

    def remove_repository(self, repo_id: str) -> GraphSnapshot:
        with self._write_lock:
            return self._publish_locked(GraphSnapshot(), set(), frozenset({repo_id}))

    def load(self) -> GraphSnapshot:
        return self.snap

    def run_cypher(self, cypher: str) -> list[dict[str, Any]]:
        raise RuntimeError("Cypher execution requires the Neo4j store")


def save_snapshot_file(snap: GraphSnapshot, path: str | Path) -> None:
    Path(path).write_text(snap.model_dump_json(indent=1), encoding="utf-8")


def load_snapshot_file(path: str | Path) -> GraphSnapshot:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw.setdefault("schema_version", 1)
        check_graph_schema(raw["schema_version"], f"snapshot {path}")
    try:
        return GraphSnapshot.model_validate(raw)
    except GraphSchemaError:
        raise
    except ValidationError as exc:
        raise GraphSchemaError(
            f"snapshot {path} cannot be read by this Axon (graph schema "
            f"{GRAPH_SCHEMA_VERSION}); it may use a renamed node or edge kind. "
            "Re-index with this Axon version to rebuild it. "
            f"Detail: {exc.errors()[0]['msg'] if exc.errors() else exc}"
        ) from exc


V1_CONSTRAINTS = [
    *[
        f"CREATE CONSTRAINT v1_{kind.value.lower()}_storage IF NOT EXISTS "
        f"FOR (n:{kind.value}) REQUIRE n._axon_storage_id IS UNIQUE"
        for kind in NodeKind
    ],
    *[
        f"CREATE INDEX v1_{kind.value.lower()}_logical IF NOT EXISTS FOR (n:{kind.value}) ON (n.id)"
        for kind in NodeKind
    ],
    "CREATE CONSTRAINT v1_publication IF NOT EXISTS "
    "FOR (n:_AxonPublication) REQUIRE n.repo_id IS UNIQUE",
    "CREATE CONSTRAINT v1_link_publication IF NOT EXISTS "
    "FOR (n:_AxonLinkPublication) REQUIRE n.id IS UNIQUE",
    "CREATE INDEX v1_link_input_generation IF NOT EXISTS "
    "FOR (n:_AxonLinkInput) ON (n.owner, n.generation)",
    "CREATE INDEX v1_link_report_generation IF NOT EXISTS "
    "FOR (n:_AxonLinkReport) ON (n.generation)",
    "CREATE INDEX v1_endpoint_path IF NOT EXISTS FOR (n:ApiEndpoint) ON (n.path)",
    "CREATE INDEX v1_symbol_name IF NOT EXISTS FOR (n:Symbol) ON (n.name)",
    "CREATE FULLTEXT INDEX v1_search IF NOT EXISTS FOR (n:ApiEndpoint|Symbol|Service) "
    "ON EACH [n.name, n.qualified, n.path]",
]
LEGACY_CONSTRAINTS = ("repo", "service", "file", "symbol", "endpoint", "spec", "client", "external")


def _active_node(variable: str) -> str:
    return active_node_predicate(variable)


_PUBLIC_CYPHER_FUNCTIONS = frozenset(
    "count collect sum avg min max percentilecont percentiledisc stdev stdevp "
    "labels type id elementid properties nodes relationships length size head last tail "
    "reverse range startnode endnode coalesce nullif isempty "
    "tolower toupper trim ltrim rtrim substring left right replace split "
    "tostring tostringornull tointeger tointegerornull tofloat tofloatornull "
    "toboolean tobooleanornull abs ceil floor round sign sqrt log log10 exp ".split()
)
_CYPHER_EXPRESSION_KEYWORDS = frozenset(
    "match optional where return with unwind in and or not case when then else distinct as".split()
)


def _guard_public_properties(query: str, masked: str) -> None:
    """Keep private values out of scalar aliases as well as returned maps.

    A narrow function allowlist excludes custom serializers/map introspection.
    Static public property subscripts and numeric list indices remain usable;
    computed property names fail closed, including aliases of properties(n).
    """
    if not masked.isascii():
        raise ValueError("generation-safe Cypher requires ASCII syntax outside strings/comments")
    if re.search(r"\.\s*_axon_\w*|\b_axon_\w*\s*:", masked):
        raise ValueError("generation-safe Cypher cannot access private properties")
    for function in re.finditer(r"\b([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)\s*\(", masked):
        name = re.sub(r"\s", "", function.group(1)).lower()
        if name not in _PUBLIC_CYPHER_FUNCTIONS | _CYPHER_EXPRESSION_KEYWORDS:
            raise ValueError(f"generation-safe Cypher does not support function {name!r}")
    for access in re.finditer(r"([A-Za-z_]\w*|\)|\]|\})\s*(\[)", masked):
        if access.group(1).lower() in _CYPHER_EXPRESSION_KEYWORDS:
            continue
        start = access.start(2)
        depth, end = 1, start + 1
        while end < len(masked) and depth:
            depth += (masked[end] == "[") - (masked[end] == "]")
            end += 1
        content = query[start + 1 : end - 1].strip()
        literal = re.fullmatch(r"(['\"])([A-Za-z_]\w*)\1", content, re.ASCII)
        numeric = re.fullmatch(r"-?\d+|-?\d*\s*\.\.\s*-?\d*", content)
        if depth or not (numeric or (literal and not literal.group(2).startswith("_axon_"))):
            raise ValueError("generation-safe Cypher requires static public property keys")


def scope_active_generations(query: str) -> str:
    """Scope nodes and bounded relationships in each read-only MATCH clause.

    Strings/comments are masked before finding clauses. Inline node predicates
    preserve OPTIONAL MATCH semantics and constrain intermediate path nodes too.
    Complex subqueries are rejected rather than executed without isolation.
    """
    masked = re.sub(
        r"'(?:(?:\\.)|[^'\\])*'|\"(?:(?:\\.)|[^\"\\])*\"|//[^\n]*|/\*.*?\*/",
        lambda m: " " * len(m.group()),
        query,
        flags=re.S,
    )
    if (
        re.search(r"\b(CALL|UNION|EXISTS)\b|\b(COUNT|COLLECT)\s*\{", masked, re.I)
        or "`" in masked
        or re.search(r"\[[^\]]*\*", masked)
    ):
        raise ValueError(
            "generation-safe Cypher does not support subqueries, quoted aliases, "
            "or variable-length paths"
        )
    _guard_public_properties(query, masked)
    # Clause words inside a node property map or inline predicate are not
    # top-level clauses (and may be ordinary property/alias names).
    depth = 0
    top_level: list[int] = []
    for offset, char in enumerate(masked):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if depth == 0:
            top_level.append(offset)
    top_offsets = set(top_level)
    clauses = [
        match
        for match in re.finditer(
            r"\b(OPTIONAL\s+MATCH|MATCH|WHERE|RETURN|WITH|UNWIND|ORDER\s+BY|SKIP|LIMIT)\b",
            masked,
            re.I,
        )
        if match.start() in top_offsets
    ]
    for index, clause in enumerate(clauses):
        if clause.group().upper().endswith("MATCH"):
            continue
        end = clauses[index + 1].start() if index + 1 < len(clauses) else len(query)
        if re.search(r"\)\s*(?:<-|-)\s*(?:\[|->|-)", masked[clause.end() : end]):
            raise ValueError("generation-safe Cypher requires graph patterns in MATCH clauses")
    edits: list[tuple[int, int, str]] = []
    aliases = set(re.findall(r"[A-Za-z_]\w*", masked))
    counters = {"node": 0, "rel": 0}

    def anonymous_alias(kind: str) -> str:
        while True:
            variable = f"_axon_{kind}_{counters[kind]}"
            counters[kind] += 1
            if variable not in aliases:
                aliases.add(variable)
                return variable

    for index, clause in enumerate(clauses):
        if not clause.group().upper().endswith("MATCH"):
            continue
        end = clauses[index + 1].start() if index + 1 < len(clauses) else len(query)
        pos = clause.end()
        pattern_mask = masked[clause.end() : end]
        if re.search(r"\b(?:shortestPath|allShortestPaths)\s*\(", pattern_mask, re.I):
            raise ValueError("generation-safe Cypher does not support shortest path patterns")
        node_ranges = []
        while pos < end:
            if masked[pos] != "(" or (
                pos and (masked[pos - 1].isalnum() or masked[pos - 1] == "_")
            ):
                pos += 1
                continue
            depth, close = 1, pos + 1
            while close < end and depth:
                depth += (masked[close] == "(") - (masked[close] == ")")
                close += 1
            if depth:
                raise ValueError("unbalanced Cypher node pattern")
            body = query[pos + 1 : close - 1]
            body_mask = masked[pos + 1 : close - 1]
            if body_mask.lstrip().startswith("("):
                raise ValueError("generation-safe Cypher does not support grouped path patterns")
            if re.search(r"\)\s*(?:<-|-)\s*(?:\[|->|-)", body_mask):
                raise ValueError("generation-safe Cypher does not support nested graph patterns")
            match = re.match(r"\s*([A-Za-z_]\w*)", body_mask)
            if match:
                variable = match.group(1)
            else:
                if body_mask.lstrip().startswith("`"):
                    raise ValueError("generation-safe Cypher requires plain node aliases")
                variable = anonymous_alias("node")
                body = variable + body
            predicate = _active_node(variable)
            existing = re.search(r"\bWHERE\b", body_mask, re.I)
            if existing:
                prefix = body[: existing.start()]
                original = body[existing.end() :]
                body = prefix + f" WHERE ({original}) AND ({predicate})"
            else:
                body += f" WHERE {predicate}"
            edits.append((pos + 1, close - 1, body))
            node_ranges.append((pos, close))
            pos = close
        guards = []
        # Each bounded connector must have a variable so its publication can
        # be guarded even when both endpoint nodes are unchanged and active.
        for (_, left_end), (right_start, _) in zip(node_ranges, node_ranges[1:]):
            connector = masked[left_end:right_start]
            if re.fullmatch(r"\s*,\s*(?:[A-Za-z_]\w*\s*=\s*)?", connector):
                continue
            rel = re.fullmatch(r"\s*(<-|-)\s*(\[[^\]]*\])?\s*(->|-)\s*", connector)
            if rel is None:
                raise ValueError("generation-safe Cypher does not support complex relationships")
            offset = left_end
            bracket = rel.group(2)
            if bracket is not None:
                if any(char in bracket[1:-1] for char in "[]()") or re.search(
                    r"\bWHERE\b", bracket, re.I
                ):
                    raise ValueError(
                        "generation-safe Cypher does not support complex relationships"
                    )
                named = re.match(r"\s*([A-Za-z_]\w*)", bracket[1:-1])
                if named:
                    variable = named.group(1)
                else:
                    variable = anonymous_alias("rel")
                    insertion = offset + rel.start(2) + 1
                    edits.append((insertion, insertion, variable))
            else:
                variable = anonymous_alias("rel")
                insertion = offset + rel.end(1)
                edits.append((insertion, insertion, f"[{variable}]"))
            guards.append(active_relationship_predicate(variable))
        if guards:
            predicate = " AND ".join(f"({guard})" for guard in guards)
            if index + 1 < len(clauses) and clauses[index + 1].group().upper() == "WHERE":
                where = clauses[index + 1]
                where_end = clauses[index + 2].start() if index + 2 < len(clauses) else len(query)
                original = query[where.end() : where_end]
                edits.append((where.end(), where_end, f" ({original}) AND {predicate} "))
            else:
                edits.append((end, end, f" WHERE {predicate} "))
    for start, end, replacement in sorted(edits, reverse=True):
        query = query[:start] + replacement + query[end:]
    return query


class DeltaNeedsFullSave(RuntimeError):
    """Legacy relationships need one full repository save before delta writes."""


def _decode_node_record(labels: list[str], props: dict[str, Any]) -> GraphNode:
    scalar = {
        "id",
        "name",
        "repo_id",
        "service_id",
        "file",
        "line_start",
        "line_end",
        "_axon_storage_id",
        "_axon_owner",
        "_axon_generation",
        "_axon_reserved_props",
        "_axon_props",
    }
    extra = (
        json.loads(props["_axon_props"])
        if "_axon_props" in props
        else {k: str(v) for k, v in props.items() if k not in scalar}
    )
    extra.update(json.loads(props.get("_axon_reserved_props", "{}")))
    extra = public_properties(extra)
    try:
        return GraphNode.model_validate(
            {
                "id": props.get("id", ""),
                "kind": labels[0],
                "name": props.get("name", ""),
                "repo_id": props.get("repo_id", ""),
                "service_id": props.get("service_id", ""),
                "file": props.get("file", ""),
                "line_start": int(props.get("line_start", 0) or 0),
                "line_end": int(props.get("line_end", 0) or 0),
                "props": extra,
            }
        )
    except ValidationError as exc:
        raise GraphSchemaError(
            "Neo4j graph cannot be read by this Axon (graph schema "
            f"{GRAPH_SCHEMA_VERSION}); it may use a renamed node kind. "
            "Re-index with this Axon version to rebuild it. "
            f"Detail: {exc.errors()[0]['msg'] if exc.errors() else exc}"
        ) from exc


def _decode_edge_record(a: str, b: str, kind: EdgeKind, p: dict[str, Any]) -> GraphEdge:
    from axon.v1.models import Evidence

    try:
        return GraphEdge(
            from_id=a,
            to_id=b,
            kind=kind,
            confidence=p.get("confidence", "EXTRACTED"),
            evidence=Evidence(
                file=p.get("file", ""),
                line=int(p.get("line", 0) or 0),
                rule=p.get("rule", ""),
                detail=p.get("detail", ""),
            ),
            props=public_properties(
                json.loads(p["_axon_props"])
                if "_axon_props" in p
                else {
                    **{
                        k: str(v)
                        for k, v in p.items()
                        if k
                        not in {
                            "confidence",
                            "file",
                            "line",
                            "rule",
                            "detail",
                            "_axon_key",
                            "_axon_reserved_props",
                        }
                    },
                    **json.loads(p.get("_axon_reserved_props", "{}")),
                }
            ),
        )
    except ValidationError as exc:
        raise GraphSchemaError(
            "Neo4j graph cannot be read by this Axon (graph schema "
            f"{GRAPH_SCHEMA_VERSION}); it may use a renamed edge kind. "
            "Re-index with this Axon version to rebuild it. "
            f"Detail: {exc.errors()[0]['msg'] if exc.errors() else exc}"
        ) from exc


class Neo4jStore:
    """Persists snapshots to Neo4j; loads hydrate a GraphSnapshot.

    The hydrated snapshot is cached on the instance (the API calls
    load() per request) and atomically replaced after successful saves.
    """

    def __init__(self) -> None:
        self._cached: GraphSnapshot | None = None
        self._load_lock = threading.RLock()
        self.last_save_s = 0.0
        self._progress_local = threading.local()
        self._physical_ids: dict[str, str] = {}
        self._generations: dict[str, str] = {}
        self.last_safety_snapshot: str = ""

    @property
    def publish_progress(self) -> Callable[[int, int], None] | None:
        return getattr(self._progress_local, "callback", None)

    @publish_progress.setter
    def publish_progress(self, callback: Callable[[int, int], None] | None) -> None:
        self._progress_local.callback = callback

    def _session(self) -> Any:
        from axon.db.connection import sync_session

        return sync_session()

    def ensure_schema(self) -> None:
        with self._session() as session:
            for name in LEGACY_CONSTRAINTS:
                session.run(f"DROP CONSTRAINT v1_{name}_id IF EXISTS")
            for stmt in V1_CONSTRAINTS:
                session.run(stmt)

    @staticmethod
    def _node_row(node: GraphNode, storage_id: str, owner: str, generation: str) -> dict[str, Any]:
        return {
            "id": node.id,
            "props": {
                **node.props,
                "id": node.id,
                "name": node.name,
                "repo_id": node.repo_id,
                "service_id": node.service_id,
                "file": node.file,
                "line_start": node.line_start,
                "line_end": node.line_end,
                "_axon_reserved_props": json.dumps(
                    {
                        key: value
                        for key, value in node.props.items()
                        if key
                        in {
                            "id",
                            "name",
                            "repo_id",
                            "service_id",
                            "file",
                            "line_start",
                            "line_end",
                            "_axon_props",
                            "_axon_storage_id",
                            "_axon_owner",
                            "_axon_generation",
                            "_axon_reserved_props",
                        }
                    }
                ),
                "_axon_storage_id": storage_id,
                "_axon_owner": owner,
                "_axon_generation": generation or None,
            },
        }

    @staticmethod
    def _edge_row(edge: GraphEdge, physical: dict[str, str]) -> dict[str, Any]:
        return {
            "a": physical.get(edge.from_id, edge.from_id),
            "b": physical.get(edge.to_id, edge.to_id),
            "props": {
                **edge.props,
                "confidence": edge.confidence.value,
                **edge.evidence.model_dump(),
                "_axon_reserved_props": json.dumps(
                    {
                        key: value
                        for key, value in edge.props.items()
                        if key
                        in {
                            "confidence",
                            "file",
                            "line",
                            "rule",
                            "detail",
                            "_axon_props",
                            "_axon_reserved_props",
                            "_axon_key",
                        }
                    }
                ),
                "_axon_key": edge_key(edge),
            },
        }

    def _run_batch(
        self, session: Any, query: str, *, label: str, rows: list[dict[str, Any]]
    ) -> None:
        try:

            def write(tx: Any) -> None:
                result = tx.run(query, rows=rows)
                if "AS written" in query:
                    record = next(iter(result), None)
                    if record is None or int(record["written"]) != len(rows):
                        raise GraphSchemaError(
                            f"staged {label} row count mismatch; publication aborted"
                        )
                self._consume(result)

            session.execute_write(write)
        except Exception as exc:
            self._raise_write_error(exc, label, len(rows))

    @staticmethod
    def _consume(result: Any) -> None:
        if callable(getattr(result, "consume", None)):
            result.consume()
        else:
            for _ in result:
                pass

    @staticmethod
    def _raise_write_error(exc: Exception, label: str, count: int) -> None:
        message = f"{type(exc).__name__}: {exc}"
        if any(
            token in message
            for token in ("MemoryPoolOutOfMemoryError", "TransientError", "TransactionMemoryLimit")
        ):
            raise RuntimeError(
                f"Neo4j publish batch {label} ({count} rows) exceeded transaction resources. "
                "Raise server.memory.heap.max_size (AXON_NEO4J_HEAP_MAX) or "
                "dbms.memory.transaction.total.max (AXON_NEO4J_TX_MAX) in development Compose; "
                "reduce NEO4J_BATCH_SIZE if memory is constrained. " + message
            ) from exc
        raise exc

    def _preflight(self, session: Any, snap: GraphSnapshot) -> None:
        # Sample rows instead of materializing a second snapshot-sized payload.
        from itertools import islice

        sample = list(islice(snap.nodes.values(), 100))
        average = sum(len(json.dumps(n.props)) + 256 for n in sample) / max(1, len(sample))
        estimate = int(min(len(snap.nodes), NEO4J_BATCH_SIZE) * average)
        try:
            settings = session.run(
                "SHOW SETTINGS YIELD name, value WHERE name = 'dbms.memory.transaction.total.max' "
                "RETURN value"
            )
            row = next(iter(settings), None)
            if row:
                value = str(row["value"]).strip().upper()
                match = re.fullmatch(r"([0-9.]+)\s*([KMGT]?)I?B?", value)
                if match:
                    limit = float(match.group(1)) * 1024 ** (
                        " KMGT".index(match.group(2)) if match.group(2) else 0
                    )
                    if limit and estimate > limit / 4:
                        logger.warning(
                            "Estimated Neo4j batch payload %d bytes approaches transaction cap %s; "
                            "AXON_NEO4J_TX_MAX controls it",
                            estimate,
                            value,
                        )
        except Exception as exc:
            logger.debug("Neo4j transaction-cap preflight unavailable: %s", exc)

    def _coordinate(
        self,
        operation: Callable[[MemoryStore], Any],
        validate: Callable[[GraphSnapshot], None] | None = None,
    ) -> GraphSnapshot:
        from axon.v1.persistent_publication import persist_candidate

        with self._load_lock:
            started = time.perf_counter()
            current = self.load()
            coordinator = MemoryStore(current)
            reconcile_started = time.perf_counter()
            operation(coordinator)
            candidate = coordinator.load()
            if validate:
                validate(candidate)
            reconcile_s = time.perf_counter() - reconcile_started
            # Safety failures abort before staging: activating a durable format
            # without the required restore artifact is not a successful migration.
            self._write_safety_snapshot(current, candidate)
            return persist_candidate(
                self, current, candidate, started=started, reconcile_s=reconcile_s
            )

    def save(self, snap: GraphSnapshot) -> None:
        self._coordinate(lambda coordinator: coordinator.save(snap))

    def apply_delta(self, delta: GraphDelta) -> None:
        self._coordinate(lambda coordinator: coordinator.apply_delta(delta))

    def publish_repository(
        self,
        replacement: GraphSnapshot,
        *,
        repo_id: str,
        validate: Callable[[GraphSnapshot], None] | None = None,
    ) -> GraphSnapshot:
        return self._coordinate(
            lambda coordinator: coordinator.publish_repository(replacement, repo_id=repo_id),
            validate,
        )

    def remove_repository(self, repo_id: str) -> GraphSnapshot:
        return self._coordinate(lambda coordinator: coordinator.remove_repository(repo_id))

    def stored_publications(self) -> list[dict[str, Any]]:
        """Published source/input pairs, including retained rollback and tombstones."""
        with self._session() as session:
            return [
                dict(row)
                for row in session.run(
                    "MATCH (p:_AxonPublication) RETURN p.repo_id AS repo_id, "
                    "p.generation AS generation, p.prev_generation AS prev_generation, "
                    "p.input_generation AS input_generation, p.prev_input_generation AS "
                    "prev_input_generation, "
                    "p.deleted AS deleted, p.schema_version AS schema_version"
                )
            ]

    def rollback_generation(self, repo_id: str | None = None) -> list[dict[str, Any]]:
        """Republish retained source/input pairs, replaying against current targets."""
        with self._load_lock:
            pubs = [
                p
                for p in self.stored_publications()
                if p.get("prev_generation") and (repo_id is None or p.get("repo_id") == repo_id)
            ]
            if not pubs:
                raise GraphSchemaError(
                    "no previous generation is retained for "
                    + (repo_id or "any repository")
                    + "; restore a safety snapshot or re-index to roll back."
                )
            retained = self._load_uncached(previous={str(p["repo_id"]) for p in pubs})
            repos = {str(p["repo_id"]) for p in pubs}
            # A previous tombstone can have no Repo row. Removal is still a fresh
            # publication; never resurrect an obsolete global outbound projection.
            present = MemoryStore._repos(retained) & repos

            def operation(coordinator: MemoryStore) -> None:
                coordinator._publish_locked(retained, present, frozenset(repos - present))

            candidate = self._coordinate(operation)
            return [
                {
                    "repo_id": p["repo_id"],
                    "generation": candidate.link_inputs[str(p["repo_id"])].source_generation
                    if p["repo_id"] in candidate.link_inputs
                    else self._generations.get(str(p["repo_id"]), ""),
                    "prev_generation": p["generation"],
                }
                for p in pubs
            ]

    def _write_safety_snapshot(self, current: GraphSnapshot, incoming: GraphSnapshot) -> None:
        if not current.nodes:
            return
        activation = current.schema_version < GRAPH_SCHEMA_VERSION
        changed = any(
            node.kind == NodeKind.REPO
            and node.id in incoming.nodes
            and (
                node.props.get("axon_build") != incoming.nodes[node.id].props.get("axon_build")
                or node.props.get("graph_schema_version")
                != incoming.nodes[node.id].props.get("graph_schema_version")
            )
            for node in current.nodes.values()
        )
        if not activation and not changed:
            return
        if not activation and os.environ.get("AXON_NO_SAFETY_SNAPSHOT", "").lower() in {
            "1",
            "true",
            "yes",
        }:
            return
        root = Path(os.environ.get("AXON_STATE_DIR", str(Path.home() / ".axon" / "state")))
        target = root / "safety" / f"graph-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Keep the actual previous schema header for old-binary restoration.
        save_snapshot_file(current, target)
        self.last_safety_snapshot = str(target)
        logger.info("wrote pre-upgrade safety snapshot to %s", target)

    def load(self) -> GraphSnapshot:
        if self._cached is not None:
            return self._cached
        with self._load_lock:
            if self._cached is not None:  # another thread won the race
                return self._cached
            started = time.perf_counter()
            snap = self._load_uncached()
            self._cached = snap
            logger.info(
                "hydrated graph from Neo4j: %d nodes, %d edges in %.1fs",
                len(snap.nodes),
                len(snap.edges),
                time.perf_counter() - started,
            )
            return snap

    def _legacy_generation_inputs(self, repo_id: str, generation: str) -> RepoLinkInputs:
        """Private pin-only read of a retained legacy source; no public graph/map edits.

        Match the source's exact physical generation and its original unversioned
        relationships. Targets may be inactive; retain their metadata in pins,
        never install them into the active graph or adopt foreign source rows.
        """
        from axon.v1.link_inputs import RepoLinkInputs
        from axon.v1.repository_links import _legacy_pin
        from axon.v1.servicemap import ServiceMap

        inputs = RepoLinkInputs(
            repo_id,
            ServiceMap(repo=repo_id),
            source_generation=generation,
            origin="legacy",
            complete=False,
        )
        with self._load_lock, self._session() as session:
            for row in session.run(
                "MATCH (a)-[r:CALLS_API]->(b) WHERE a._axon_owner = $repo_id "
                "AND a._axon_generation = $generation AND r._axon_link_generation IS NULL "
                "RETURN a.id AS a, b.id AS b, labels(b) AS labels, "
                "properties(b) AS target, properties(r) AS props",
                repo_id=repo_id,
                generation=generation,
            ):
                target = _decode_node_record(row["labels"], dict(row["target"]))
                edge = _decode_edge_record(
                    row["a"], row["b"], EdgeKind.CALLS_API, dict(row["props"])
                )
                inputs.pinned.append(_legacy_pin(edge, target))
        return inputs

    @staticmethod
    def _link_token(session: Any) -> str:
        rows = session.run(
            "MATCH (p:_AxonLinkPublication {id:'global'}) RETURN p.generation AS generation"
        )
        row = next(iter(rows), None)
        return str(row.get("generation") or "") if row else ""

    def _load_uncached(self, *, previous: set[str] | None = None) -> GraphSnapshot:
        # Neo4j read-committed isolation is not a multi-statement snapshot.
        # Pin physical versions, then verify the monotonic publication token;
        # retry if a writer crossed the hydration boundary (including GC races).
        with self._session() as session:
            for _ in range(3):
                token = self._link_token(session)
                physical, generations = dict(self._physical_ids), dict(self._generations)
                try:
                    snap = self._hydrate_uncached(session, previous)
                except Exception:
                    if self._link_token(session) == token:
                        self._physical_ids, self._generations = physical, generations
                        raise
                else:
                    if self._link_token(session) == token and snap.publication_id == token:
                        return snap
                self._physical_ids, self._generations = physical, generations
        raise GraphSchemaError("graph changed during hydration; retry the read")

    def _hydrate_uncached(self, session: Any, previous: set[str] | None) -> GraphSnapshot:
        snap = GraphSnapshot()
        publications = [
            dict(pub)
            for pub in session.run(
                "MATCH (p:_AxonPublication) RETURN p.repo_id AS repo_id, p.generation AS "
                "generation, "
                "p.input_generation AS input_generation, p.prev_generation AS prev_generation, "
                "p.prev_input_generation AS prev_input_generation, p.schema_version AS "
                "schema_version"
            )
        ]
        globals_ = list(
            session.run(
                "MATCH (p:_AxonLinkPublication {id:'global'}) RETURN properties(p) AS props"
            )
        )
        global_props = dict(globals_[0]["props"]) if globals_ else {}
        versions = []
        schema = 1
        for pub in [*publications, global_props]:
            if pub.get("schema_version") is not None:
                schema = max(schema, check_graph_schema(pub["schema_version"], "Neo4j graph"))
        snap.schema_version = schema
        for pub in publications:
            old = previous is not None and pub["repo_id"] in previous
            versions.append(
                {
                    "repo_id": pub["repo_id"],
                    "generation": pub.get("prev_generation" if old else "generation"),
                    "input_generation": pub.get(
                        "prev_input_generation" if old else "input_generation"
                    ),
                }
            )
        params = {"versions": versions, "link_generation": global_props.get("generation")}

        def active(variable: str) -> str:
            return (
                f"NONE(label IN labels({variable}) WHERE label STARTS WITH '_Axon') AND ("
                f"({variable}:ExternalApi AND (({variable}._axon_link_generation IS NULL AND "
                f"$link_generation IS NULL) "
                f"OR {variable}._axon_link_generation = $link_generation)) OR "
                f"(NOT {variable}:ExternalApi AND (({variable}._axon_generation IS NULL "
                f"AND NONE(p IN $versions WHERE p.repo_id = {variable}.repo_id)) OR "
                f"ANY(p IN $versions WHERE p.repo_id = {variable}._axon_owner AND "
                f"p.generation = {variable}._axon_generation))))"
            )

        def relationship(variable: str) -> str:
            return (
                f"(type({variable}) <> 'CALLS_API' OR "
                f"({variable}._axon_link_generation IS NULL AND $link_generation IS NULL) "
                f"OR {variable}._axon_link_generation = $link_generation)"
            )

        # Iterate the cursor record-by-record: .data() would first
        # materialize millions of row dicts on top of the snapshot
        # being built, roughly doubling peak memory.
        for rec in session.run(
            "MATCH (n) WHERE "
            + active("n")
            + " RETURN labels(n) AS labels, properties(n) AS props",
            **params,
        ):
            props = dict(rec["props"])
            if props.get("_axon_storage_id") and previous is None:
                self._physical_ids[props["id"]] = props["_axon_storage_id"]
                if props.get("_axon_generation"):
                    self._generations[props["_axon_owner"]] = props["_axon_generation"]
            snap.add_node(_decode_node_record(rec["labels"], props))
        for rec in session.run(
            "MATCH (a)-[r]->(b) WHERE "
            + active("a")
            + " AND "
            + active("b")
            + " AND "
            + relationship("r")
            + " RETURN a.id AS a, b.id AS b, "
            "type(r) AS kind, properties(r) AS props",
            **params,
        ):
            snap.add_edge(_decode_edge_record(rec["a"], rec["b"], rec["kind"], rec["props"]))
        input_rows = [
            dict(rec["props"])
            for rec in session.run(
                "MATCH (n:_AxonLinkInput) WHERE ANY(p IN $versions WHERE "
                "p.repo_id = n.owner AND p.input_generation = n.generation) RETURN "
                "properties(n) AS props",
                **params,
            )
        ]
        snap.link_inputs = decode_link_inputs(input_rows)
        expected_inputs = {p["repo_id"] for p in versions if p.get("input_generation")}
        if set(snap.link_inputs) != expected_inputs:
            raise GraphSchemaError("published link input metadata is missing; restore or re-index")
        report_rows = [
            dict(rec["props"])
            for rec in session.run(
                "MATCH (n:_AxonLinkReport) WHERE n.generation = $link_generation RETURN "
                "properties(n) AS props",
                **params,
            )
        ]
        snap.link_reports = decode_link_reports(report_rows)
        if set(snap.link_reports) != set(global_props.get("report_repos", [])):
            raise GraphSchemaError("published link report metadata is missing; restore or re-index")
        snap.publication_id = global_props.get("generation", "")
        snap.commit = global_props.get("commit", "")
        if global_props.get("snapshot_report"):
            snapshot_rows = [
                dict(rec["props"])
                for rec in session.run(
                    "MATCH (n:_AxonSnapshotReport) WHERE n.generation = $link_generation "
                    "RETURN properties(n) AS props",
                    **params,
                )
            ]
            aggregate = decode_link_reports(snapshot_rows)
            if set(aggregate) != {"snapshot"}:
                raise GraphSchemaError(
                    "published snapshot report metadata is missing; restore or re-index"
                )
            snap.unresolved = aggregate["snapshot"].unresolved
            snap.diagnostics = aggregate["snapshot"].diagnostics
        else:
            # Reader-only synthetic/legacy markers predate the production writer.
            snap.unresolved = list(global_props.get("unresolved", []))
            snap.diagnostics = list(global_props.get("diagnostics", []))
        return snap

    def run_cypher(self, cypher: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            scoped = scope_active_generations(cypher)
        except ValueError as exc:
            raise QueryValidationError(str(exc)) from None
        try:
            with self._load_lock, self._session() as session:
                for pub in session.run(
                    "MATCH (p) WHERE p:_AxonPublication OR p:_AxonLinkPublication RETURN "
                    "p.schema_version AS schema_version"
                ):
                    if pub.get("schema_version") is not None:
                        check_graph_schema(pub["schema_version"], "Neo4j graph")
                for rec in session.run(scoped):
                    if len(rows) >= 100:
                        break
                    data = public_query_value(dict(rec))
                    # Neo4j values are not all JSON scalar; stringify nested values.
                    rows.append(
                        {
                            k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v))
                            for k, v in data.items()
                        }
                    )
        except GraphSchemaError:
            raise
        except Exception as exc:
            # Neo4j type errors may echo entire physical maps in their message.
            # Use only fixed categories; even unknown driver codes stay private.
            code = getattr(exc, "code", "")
            category = (
                {
                    "Neo.ClientError.Statement.TypeError": (
                        "type error; check operand and result types"
                    ),
                    "Neo.ClientError.Statement.SyntaxError": (
                        "syntax error; check the read-only query syntax"
                    ),
                    "Neo.ClientError.Statement.SemanticError": (
                        "semantic error; check names and supported expressions"
                    ),
                }.get(code, "execution error; check query types, syntax, and database availability")
                if isinstance(code, str)
                else "execution error; check database availability"
            )
            raise QueryExecutionError(f"Graph query {category}.") from None
        return rows


def cli_init() -> None:
    """`axon-init`: create v1 Neo4j constraints/indexes (idempotent)."""
    import logging as _logging

    _logging.basicConfig(level="INFO")
    Neo4jStore().ensure_schema()
    print("Axon v1 Neo4j schema ready.")


def make_store(backend: str, *, ensure_schema: bool = True) -> GraphStore:
    if backend == "neo4j":
        store = Neo4jStore()
        if not ensure_schema:
            return store
        try:
            store.ensure_schema()
        except Exception as exc:
            logger.warning("Neo4j unavailable (%s); falling back to memory", exc)
            return MemoryStore()
        return store
    return MemoryStore()
