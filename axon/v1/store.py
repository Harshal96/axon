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
from typing import Any, Protocol, TypeVar

from pydantic import ValidationError

from axon.v1.graph import GraphSnapshot
from axon.v1.graph_delta import GraphDelta, edge_key, replace_repositories
from axon.v1.models import (
    GRAPH_SCHEMA_VERSION,
    GraphEdge,
    GraphNode,
    GraphSchemaError,
    NodeKind,
    check_graph_schema,
)

logger = logging.getLogger(__name__)

NEO4J_BATCH_SIZE = 5000


_T = TypeVar("_T")


def _chunks(items: list[_T], size: int) -> Iterator[list[_T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class GraphStore(Protocol):
    def save(self, snap: GraphSnapshot) -> None: ...
    def load(self) -> GraphSnapshot: ...
    def run_cypher(self, cypher: str) -> list[dict[str, Any]]: ...


class MemoryStore:
    def __init__(self, snap: GraphSnapshot | None = None) -> None:
        self.snap = snap or GraphSnapshot()
        self._write_lock = threading.Lock()
        self.last_save_s = 0.0

    def save(self, snap: GraphSnapshot) -> None:
        """Replace repos present in the incoming snapshot.

        Takes ownership of the incoming snapshot's nodes and edges;
        callers must not mutate ``snap`` after saving.
        """
        started = time.perf_counter()
        with self._write_lock:
            self.snap = replace_repositories(self.snap, snap)
        self.last_save_s = time.perf_counter() - started

    def apply_delta(self, delta: GraphDelta) -> None:
        """Publish a delta without mutating any previously returned snapshot."""
        started = time.perf_counter()
        with self._write_lock:
            self.snap = delta.apply(self.snap)
        self.last_save_s = time.perf_counter() - started

    def load(self) -> GraphSnapshot:
        return self.snap

    def run_cypher(self, cypher: str) -> list[dict[str, Any]]:
        raise RuntimeError("Cypher execution requires the Neo4j store")


def save_snapshot_file(snap: GraphSnapshot, path: str | Path) -> None:
    Path(path).write_text(snap.model_dump_json(indent=1), encoding="utf-8")


def load_snapshot_file(path: str | Path) -> GraphSnapshot:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "schema_version" in raw:
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
    "CREATE INDEX v1_endpoint_path IF NOT EXISTS FOR (n:ApiEndpoint) ON (n.path)",
    "CREATE INDEX v1_symbol_name IF NOT EXISTS FOR (n:Symbol) ON (n.name)",
    "CREATE FULLTEXT INDEX v1_search IF NOT EXISTS FOR (n:ApiEndpoint|Symbol|Service) "
    "ON EACH [n.name, n.qualified, n.path]",
]
LEGACY_CONSTRAINTS = ("repo", "service", "file", "symbol", "endpoint", "spec", "client", "external")


def _active_node(variable: str) -> str:
    # Legacy rows remain readable until that repository publishes its first generation.
    return (
        f"NOT {variable}:_AxonPublication AND ("
        f"({variable}._axon_generation IS NULL AND NOT EXISTS {{ "
        f"MATCH (_axon_pub:_AxonPublication) WHERE _axon_pub.repo_id = {variable}.repo_id }}) "
        f"OR EXISTS {{ MATCH (_axon_pub:_AxonPublication) "
        f"WHERE _axon_pub.repo_id = {variable}._axon_owner "
        f"AND _axon_pub.generation = {variable}._axon_generation }})"
    )


def scope_active_generations(query: str) -> str:
    """Scope node patterns in read-only MATCH clauses to published generations.

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
            r"\b(MATCH|WHERE|RETURN|WITH|UNWIND|ORDER\s+BY|SKIP|LIMIT)\b", masked, re.I
        )
        if match.start() in top_offsets
    ]
    for index, clause in enumerate(clauses):
        if clause.group().upper() == "MATCH":
            continue
        end = clauses[index + 1].start() if index + 1 < len(clauses) else len(query)
        if re.search(r"-->|<--|--|\)-?\[", masked[clause.end() : end]):
            raise ValueError("generation-safe Cypher requires graph patterns in MATCH clauses")
    edits: list[tuple[int, int, str]] = []
    anonymous = 0
    for index, clause in enumerate(clauses):
        if clause.group().upper() != "MATCH":
            continue
        end = clauses[index + 1].start() if index + 1 < len(clauses) else len(query)
        pos = clause.end()
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
            match = re.match(r"\s*([A-Za-z_]\w*)", body_mask)
            if match:
                variable = match.group(1)
            else:
                if body_mask.lstrip().startswith("`"):
                    raise ValueError("generation-safe Cypher requires plain node aliases")
                variable = f"_axon_node_{anonymous}"
                anonymous += 1
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
            pos = close
    for start, end, replacement in reversed(edits):
        query = query[:start] + replacement + query[end:]
    return query


class DeltaNeedsFullSave(RuntimeError):
    """Legacy relationships need one full repository save before delta writes."""


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
            session.execute_write(lambda tx: self._consume(tx.run(query, rows=rows)))
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

    def save(self, snap: GraphSnapshot) -> None:
        # One publication lock also protects the read/compare/write sequence from
        # losing a concurrently published *different* repository in the cache.
        with self._load_lock:
            self._save_locked(snap)

    def _save_locked(self, snap: GraphSnapshot) -> None:
        """Diff re-indexes; stage large replacements in bounded transactions.

        A publication marker makes complete staged generations live atomically.
        Failed staging leaves the prior publication and cache untouched. Cleanup
        is bounded and occurs only after publication.
        """
        started = time.perf_counter()
        current = self.load()
        try:
            self._write_safety_snapshot(current, snap)
        except Exception as exc:  # safety copy must never block publishing
            logger.warning("pre-upgrade safety snapshot failed: %s", exc)
        candidate = replace_repositories(current, snap)
        delta = GraphDelta.between(current, candidate)
        incoming_repos = {n.repo_id for n in snap.nodes.values() if n.repo_id}
        current_repos = {n.repo_id for n in current.nodes.values() if n.repo_id}
        if (
            current.nodes
            and incoming_repos <= current_repos
            and self._delta_size(delta) <= NEO4J_BATCH_SIZE
        ):
            try:
                self.apply_delta(delta)
                return
            except DeltaNeedsFullSave:
                pass
        del candidate, delta
        with self._load_lock:
            self._publish_generation(snap)
            # Allocate the replacement containers after the database commit.
            self._cached = replace_repositories(current, snap)
            self._physical_ids = {
                key: value for key, value in self._physical_ids.items() if key in self._cached.nodes
            }
        self.last_save_s = time.perf_counter() - started

    @staticmethod
    def _delta_size(delta: GraphDelta) -> int:
        return (
            len(delta.upsert_nodes)
            + len(delta.remove_node_ids)
            + len(delta.add_edges)
            + len(delta.remove_edge_keys)
        )

    def stored_publications(self) -> list[dict[str, Any]]:
        """Published generation markers, read without writing any schema."""
        with self._session() as session:
            rows = session.run(
                "MATCH (p:_AxonPublication) RETURN p.repo_id AS repo_id, "
                "p.generation AS generation, p.prev_generation AS prev_generation, "
                "p.schema_version AS schema_version"
            )
            return [dict(row) for row in rows]

    def rollback_generation(self, repo_id: str | None = None) -> list[dict[str, Any]]:
        """Flip publication markers back to the retained previous generation."""
        with self._load_lock:
            pubs = [
                pub
                for pub in self.stored_publications()
                if pub.get("prev_generation") and (repo_id is None or pub.get("repo_id") == repo_id)
            ]
            if not pubs:
                raise GraphSchemaError(
                    "no previous generation is retained for "
                    + (repo_id or "any repository")
                    + "; re-index with the previous Axon version to roll back."
                )
            rows = [
                {
                    "repo_id": pub["repo_id"],
                    "generation": pub["prev_generation"],
                    "prev_generation": pub["generation"],
                }
                for pub in pubs
            ]
            with self._session() as session:
                session.execute_write(
                    lambda tx: self._consume(
                        tx.run(
                            "UNWIND $rows AS row MATCH (p:_AxonPublication "
                            "{repo_id: row.repo_id}) SET p.generation = row.generation, "
                            "p.prev_generation = row.prev_generation",
                            rows=rows,
                        )
                    )
                )
            for row in rows:
                self._generations[row["repo_id"]] = row["generation"]
            self._cached = None
            return rows

    def _write_safety_snapshot(self, current: GraphSnapshot, incoming: GraphSnapshot) -> None:
        if os.environ.get("AXON_NO_SAFETY_SNAPSHOT", "").lower() in {"1", "true", "yes"}:
            return
        if not current.nodes:
            return
        changed = False
        for node in current.nodes.values():
            if node.kind != NodeKind.REPO:
                continue
            other = incoming.nodes.get(node.id)
            if other is None:
                continue
            if node.props.get("axon_build") != other.props.get("axon_build") or node.props.get(
                "graph_schema_version"
            ) != other.props.get("graph_schema_version"):
                changed = True
        if not changed:
            return
        root = Path(os.environ.get("AXON_STATE_DIR", str(Path.home() / ".axon" / "state")))
        target = root / "safety" / f"graph-{time.strftime('%Y%m%dT%H%M%S')}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        save_snapshot_file(current, target)
        self.last_safety_snapshot = str(target)
        logger.info("wrote pre-upgrade safety snapshot to %s", target)

    def _publish_generation(
        self, snap: GraphSnapshot, *, repository_ids: set[str] | None = None
    ) -> None:
        repos = sorted(repository_ids or {n.repo_id for n in snap.nodes.values() if n.repo_id})
        if not repos:
            return
        prev_by_repo: dict[str, str] = {}
        try:
            prev_by_repo = {
                str(pub["repo_id"]): str(pub["generation"])
                for pub in self.stored_publications()
                if pub.get("repo_id") and pub.get("generation")
            }
        except Exception:
            prev_by_repo = {}
        generation = uuid.uuid4().hex
        keep_generations = sorted(
            {generation, *(prev_by_repo[r] for r in repos if r in prev_by_repo)}
        )
        generations = {repo: generation for repo in repos}
        physical = dict(self._physical_ids)
        owners = {n.id: n.repo_id or repos[0] for n in snap.nodes.values()}
        physical.update(
            {
                node.id: f"shared:{node.id}"
                if node.kind == NodeKind.EXTERNAL_API and not node.repo_id
                else f"{generation}:{node.id}"
                for node in snap.nodes.values()
            }
        )
        labels: dict[str, list[GraphNode]] = {}
        for node in snap.nodes.values():
            labels.setdefault(node.kind.value, []).append(node)
        shapes: dict[tuple[str, str, str], list[GraphEdge]] = {}
        label_of = {n.id: n.kind.value for n in snap.nodes.values()}
        for edge in snap.edges:
            shapes.setdefault(
                (label_of.get(edge.from_id, ""), label_of.get(edge.to_id, ""), edge.kind.value), []
            ).append(edge)
        total = (
            sum((len(rows) + NEO4J_BATCH_SIZE - 1) // NEO4J_BATCH_SIZE for rows in labels.values())
            + sum(
                (len(edges) + NEO4J_BATCH_SIZE - 1) // NEO4J_BATCH_SIZE for edges in shapes.values()
            )
            + 1
        )
        done = 0
        with self._session() as session:
            self._preflight(session, snap)
            for label, nodes in labels.items():
                for chunk in _chunks(nodes, NEO4J_BATCH_SIZE):
                    rows = [
                        self._node_row(
                            n,
                            physical[n.id],
                            ""
                            if n.kind == NodeKind.EXTERNAL_API and not n.repo_id
                            else owners[n.id],
                            "" if n.kind == NodeKind.EXTERNAL_API and not n.repo_id else generation,
                        )
                        for n in chunk
                    ]
                    self._run_batch(
                        session,
                        (
                            f"UNWIND $rows AS row MERGE (x:{label} "
                            "{_axon_storage_id: row.props._axon_storage_id}) "
                            "SET x = row.props"
                            if label == NodeKind.EXTERNAL_API.value
                            else f"UNWIND $rows AS row CREATE (x:{label}) SET x = row.props"
                        ),
                        label=label,
                        rows=rows,
                    )
                    done += 1
                    if self.publish_progress:
                        self.publish_progress(done, total)
            for (a_label, b_label, kind), edges in shapes.items():
                a = f"a:{a_label}" if a_label else "a"
                b = f"b:{b_label}" if b_label else "b"
                for edge_chunk in _chunks(edges, NEO4J_BATCH_SIZE):
                    rows = [self._edge_row(edge, physical) for edge in edge_chunk]
                    self._run_batch(
                        session,
                        f"UNWIND $rows AS row MATCH ({a} {{_axon_storage_id: row.a}}), "
                        f"({b} {{_axon_storage_id: row.b}}) "
                        f"CREATE (a)-[r:{kind}]->(b) SET r = row.props",
                        label=kind,
                        rows=rows,
                    )
                    done += 1
                    if self.publish_progress:
                        self.publish_progress(done, total)
            self._run_batch(
                session,
                "UNWIND $rows AS row MERGE (p:_AxonPublication {repo_id: row.repo_id}) "
                "SET p.generation = row.generation, "
                "p.prev_generation = row.prev_generation, "
                "p.schema_version = row.schema_version",
                label="publication",
                rows=[
                    {
                        "repo_id": repo,
                        "generation": generation,
                        "prev_generation": prev_by_repo.get(repo),
                        "schema_version": GRAPH_SCHEMA_VERSION,
                    }
                    for repo in repos
                ],
            )
            self._physical_ids = physical
            self._generations.update(generations)
            if self.publish_progress:
                self.publish_progress(total, total)
            # Cleanup failure does not turn a committed publication into a failed job.
            try:
                for kind in NodeKind:
                    # A small node batch can still have millions of relationships.
                    # Remove old-generation relationships in bounded transactions first.
                    while True:
                        result = session.execute_write(
                            lambda tx, label=kind.value: list(
                                tx.run(
                                    f"MATCH (n:{label})-[r]-() WHERE "
                                    "(n.repo_id IN $repos OR n._axon_owner IN $repos) "
                                    "AND (n._axon_generation IS NULL "
                                    "OR NOT n._axon_generation IN $keep_generations) "
                                    "WITH DISTINCT r LIMIT $batch DELETE r "
                                    "RETURN count(*) AS removed",
                                    repos=repos,
                                    keep_generations=keep_generations,
                                    batch=NEO4J_BATCH_SIZE,
                                )
                            )
                        )
                        if not result or int(result[0]["removed"]) < NEO4J_BATCH_SIZE:
                            break
                    while True:
                        result = session.execute_write(
                            lambda tx, label=kind.value: list(
                                tx.run(
                                    f"MATCH (n:{label}) WHERE "
                                    "(n.repo_id IN $repos OR n._axon_owner IN $repos) "
                                    "AND (n._axon_generation IS NULL "
                                    "OR NOT n._axon_generation IN $keep_generations) "
                                    "WITH n LIMIT $batch DETACH DELETE n "
                                    "RETURN count(*) AS removed",
                                    repos=repos,
                                    keep_generations=keep_generations,
                                    batch=NEO4J_BATCH_SIZE,
                                )
                            )
                        )
                        if not result or int(result[0]["removed"]) < NEO4J_BATCH_SIZE:
                            break
                # Shared placeholders have stable identities outside generations.
                # Collect only degree-zero rows after all obsolete edges are gone;
                # another repository's live callers keep their placeholder alive.
                while True:
                    result = session.execute_write(
                        lambda tx: list(
                            tx.run(
                                "MATCH (n:ExternalApi) WHERE coalesce(n.repo_id, '') = '' "
                                "AND NOT (n)--() WITH n LIMIT $batch DELETE n "
                                "RETURN count(*) AS removed",
                                batch=NEO4J_BATCH_SIZE,
                            )
                        )
                    )
                    if not result or int(result[0]["removed"]) < NEO4J_BATCH_SIZE:
                        break
            except Exception as exc:
                logger.warning("Published generation; deferred obsolete-row cleanup: %s", exc)

    def apply_delta(self, delta: GraphDelta) -> None:
        with self._load_lock:
            self._apply_delta_locked(delta)

    def _apply_delta_locked(self, delta: GraphDelta) -> None:
        """Apply a small delta atomically; large changes use staged publication."""
        started = time.perf_counter()
        if self._delta_size(delta) > NEO4J_BATCH_SIZE:
            current = self.load()
            target = delta.apply(current)
            changed_repos = {n.repo_id for n in delta.upsert_nodes.values() if n.repo_id}
            changed_repos.update(
                current.nodes[node_id].repo_id
                for node_id in delta.remove_node_ids
                if node_id in current.nodes
            )
            if not changed_repos:
                changed_repos = {n.repo_id for n in current.nodes.values() if n.repo_id}
            scoped_ids = {
                node_id
                for node_id, n in target.nodes.items()
                if n.repo_id in changed_repos or not n.repo_id
            }
            scoped = GraphSnapshot(
                nodes={key: target.nodes[key] for key in scoped_ids},
                edges=[
                    e for e in target.edges if e.from_id in scoped_ids and e.to_id in scoped_ids
                ],
                unresolved=target.unresolved,
                diagnostics=target.diagnostics,
                commit=target.commit,
            )
            with self._load_lock:
                self._publish_generation(scoped, repository_ids=changed_repos)
                self._cached = target
                self._physical_ids = {
                    key: value for key, value in self._physical_ids.items() if key in target.nodes
                }
            self.last_save_s = time.perf_counter() - started
            return
        affected_ids = set(delta.upsert_nodes) | delta.remove_node_ids
        for key in delta.remove_edge_keys:
            data = json.loads(key)
            affected_ids.update((data["from_id"], data["to_id"]))
        for edge in delta.add_edges:
            affected_ids.update((edge.from_id, edge.to_id))
        physical = dict(self._physical_ids)
        repos = sorted({n.repo_id for n in delta.upsert_nodes.values() if n.repo_id})
        for node in delta.upsert_nodes.values():
            owner = node.repo_id or (repos[0] if repos else "")
            generation = self._generations.get(owner, "legacy")
            if node.kind == NodeKind.EXTERNAL_API and not node.repo_id:
                physical[node.id] = f"shared:{node.id}"
            else:
                physical.setdefault(node.id, f"{generation}:{node.id}")

        def write(tx: Any) -> None:
            legacy = tx.run(
                "MATCH (a)-[r]->(b) WHERE r._axon_key IS NULL "
                "AND (a.id IN $ids OR b.id IN $ids) RETURN 1 AS legacy LIMIT 1",
                ids=sorted(affected_ids),
            )
            if next(iter(legacy), None) is not None:
                raise DeltaNeedsFullSave(
                    "Affected relationships lack delta keys; save the full repository snapshot"
                )
            legacy_nodes = tx.run(
                "MATCH (n) WHERE n.id IN $ids AND n._axon_storage_id IS NULL "
                "RETURN 1 AS legacy LIMIT 1",
                ids=sorted(affected_ids),
            )
            if next(iter(legacy_nodes), None) is not None:
                raise DeltaNeedsFullSave(
                    "Affected nodes lack generation keys; save the full repository snapshot"
                )
            for keys in _chunks(sorted(delta.remove_edge_keys), NEO4J_BATCH_SIZE):
                self._consume(
                    tx.run(
                        "MATCH (a)-[r]->(b) WHERE r._axon_key IN $keys AND "
                        + _active_node("a")
                        + " AND "
                        + _active_node("b")
                        + " DELETE r",
                        keys=keys,
                    )
                )
            for kind in NodeKind:
                if delta.remove_node_ids:
                    self._consume(
                        tx.run(
                            f"MATCH (n:{kind.value}) WHERE n.id IN $ids AND "
                            + _active_node("n")
                            + " DETACH DELETE n",
                            ids=sorted(delta.remove_node_ids),
                        )
                    )
            labels: dict[str, list[GraphNode]] = {}
            for node in delta.upsert_nodes.values():
                labels.setdefault(node.kind.value, []).append(node)
            for label, nodes in labels.items():
                rows = [
                    self._node_row(
                        n,
                        physical[n.id],
                        ""
                        if n.kind == NodeKind.EXTERNAL_API and not n.repo_id
                        else n.repo_id or (repos[0] if repos else ""),
                        ""
                        if n.kind == NodeKind.EXTERNAL_API and not n.repo_id
                        else self._generations.get(
                            n.repo_id or (repos[0] if repos else ""), "legacy"
                        ),
                    )
                    for n in nodes
                ]
                try:
                    self._consume(
                        tx.run(
                            f"UNWIND $rows AS row MERGE (x:{label} "
                            "{_axon_storage_id: row.props._axon_storage_id}) SET x = row.props",
                            rows=rows,
                        )
                    )
                except Exception as exc:
                    self._raise_write_error(exc, label, len(rows))
            shapes: dict[tuple[str, str, str], list[GraphEdge]] = {}
            for edge in delta.add_edges:

                def label_for(node_id: str) -> str:
                    node = delta.upsert_nodes.get(node_id)
                    if node is None and self._cached is not None:
                        node = self._cached.nodes.get(node_id)
                    return node.kind.value if node is not None else ""

                shape = (label_for(edge.from_id), label_for(edge.to_id), edge.kind.value)
                shapes.setdefault(shape, []).append(edge)
            for (from_label, to_label, edge_kind), edges in shapes.items():
                rows = [self._edge_row(edge, physical) for edge in edges]
                a = f"a:{from_label}" if from_label else "a"
                b = f"b:{to_label}" if to_label else "b"
                try:
                    self._consume(
                        tx.run(
                            f"UNWIND $rows AS row MATCH ({a} {{_axon_storage_id: row.a}}), "
                            f"({b} {{_axon_storage_id: row.b}}) "
                            f"CREATE (a)-[r:{edge_kind}]->(b) SET r = row.props",
                            rows=rows,
                        )
                    )
                except Exception as exc:
                    self._raise_write_error(exc, edge_kind, len(rows))

        with self._load_lock:
            with self._session() as session:
                session.execute_write(write)
            self._cached = delta.apply(self._cached) if self._cached is not None else None
            for node_id in delta.remove_node_ids:
                physical.pop(node_id, None)
            self._physical_ids = physical
        if self.publish_progress:
            self.publish_progress(1, 1)
        self.last_save_s = time.perf_counter() - started

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

    def _load_uncached(self) -> GraphSnapshot:
        from axon.v1.models import Evidence

        snap = GraphSnapshot()
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
        with self._session() as session:
            for pub in session.run(
                "MATCH (p:_AxonPublication) RETURN p.repo_id AS repo_id, "
                "p.schema_version AS schema_version"
            ):
                if pub.get("schema_version") is not None:
                    check_graph_schema(
                        pub["schema_version"], f"Neo4j graph for {pub.get('repo_id') or 'repo'}"
                    )
            # Iterate the cursor record-by-record: .data() would first
            # materialize millions of row dicts on top of the snapshot
            # being built, roughly doubling peak memory.
            for rec in session.run(
                "MATCH (n) WHERE "
                + _active_node("n")
                + " RETURN labels(n) AS labels, properties(n) AS props"
            ):
                props = dict(rec["props"])
                if props.get("_axon_storage_id"):
                    self._physical_ids[props["id"]] = props["_axon_storage_id"]
                    if props.get("_axon_generation"):
                        self._generations[props["_axon_owner"]] = props["_axon_generation"]
                extra = (
                    json.loads(props["_axon_props"])
                    if "_axon_props" in props
                    else {k: str(v) for k, v in props.items() if k not in scalar}
                )
                extra.update(json.loads(props.get("_axon_reserved_props", "{}")))
                try:
                    node = GraphNode(
                        id=props.get("id", ""),
                        kind=rec["labels"][0],
                        name=props.get("name", ""),
                        repo_id=props.get("repo_id", ""),
                        service_id=props.get("service_id", ""),
                        file=props.get("file", ""),
                        line_start=int(props.get("line_start", 0) or 0),
                        line_end=int(props.get("line_end", 0) or 0),
                        props=extra,
                    )
                except ValidationError as exc:
                    raise GraphSchemaError(
                        "Neo4j graph cannot be read by this Axon (graph schema "
                        f"{GRAPH_SCHEMA_VERSION}); it may use a renamed node kind. "
                        "Re-index with this Axon version to rebuild it. "
                        f"Detail: {exc.errors()[0]['msg'] if exc.errors() else exc}"
                    ) from exc
                snap.add_node(node)
            for rec in session.run(
                "MATCH (a)-[r]->(b) WHERE "
                + _active_node("a")
                + " AND "
                + _active_node("b")
                + " RETURN a.id AS a, b.id AS b, "
                "type(r) AS kind, properties(r) AS props"
            ):
                p = rec["props"]
                try:
                    edge = GraphEdge(
                        from_id=rec["a"],
                        to_id=rec["b"],
                        kind=rec["kind"],
                        confidence=p.get("confidence", "EXTRACTED"),
                        evidence=Evidence(
                            file=p.get("file", ""),
                            line=int(p.get("line", 0) or 0),
                            rule=p.get("rule", ""),
                            detail=p.get("detail", ""),
                        ),
                        props=(
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
                snap.add_edge(edge)
        return snap

    def run_cypher(self, cypher: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self._load_lock, self._session() as session:
            for rec in session.run(scope_active_generations(cypher)):
                if len(rows) >= 100:
                    break
                data = rec.data() if callable(getattr(rec, "data", None)) else dict(rec)
                # Neo4j values are not all JSON scalar; stringify nested values.
                rows.append(
                    {
                        k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v))
                        for k, v in data.items()
                    }
                )
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
