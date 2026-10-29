"""Graph stores: in-memory (tests/CLI), snapshot files, and Neo4j."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol, TypeVar

from axon.v2.graph import GraphSnapshot
from axon.v2.models import GraphEdge, GraphNode

logger = logging.getLogger(__name__)

NEO4J_BATCH_SIZE = 5000
NEO4J_DELETE_BATCH_SIZE = 10000


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

    def save(self, snap: GraphSnapshot) -> None:
        """Replace repos present in the incoming snapshot.

        Takes ownership of the incoming snapshot's nodes and edges;
        callers must not mutate ``snap`` after saving.
        """
        repos = {n.repo_id for n in snap.nodes.values() if n.repo_id}
        drop = {nid for nid, n in self.snap.nodes.items() if n.repo_id in repos}
        for nid in drop:
            del self.snap.nodes[nid]
        self.snap._kind_cache.clear()
        self.snap.edges = [
            e for e in self.snap.edges if e.from_id not in drop and e.to_id not in drop
        ]
        for n in snap.nodes.values():
            self.snap.add_node(n)
        self.snap.edges.extend(snap.edges)
        self.snap.unresolved = snap.unresolved
        self.snap.diagnostics = snap.diagnostics
        self.snap.dedupe_edges()
        self.snap._invalidate_edge_indexes()

    def load(self) -> GraphSnapshot:
        return self.snap

    def run_cypher(self, cypher: str) -> list[dict[str, Any]]:
        raise RuntimeError("Cypher execution requires the Neo4j store")


def save_snapshot_file(snap: GraphSnapshot, path: str | Path) -> None:
    Path(path).write_text(snap.model_dump_json(indent=1), encoding="utf-8")


def load_snapshot_file(path: str | Path) -> GraphSnapshot:
    return GraphSnapshot.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


V2_CONSTRAINTS = [
    "CREATE CONSTRAINT v2_repo_id IF NOT EXISTS FOR (n:Repo) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT v2_service_id IF NOT EXISTS FOR (n:Service) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT v2_file_id IF NOT EXISTS FOR (n:SourceFile) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT v2_symbol_id IF NOT EXISTS FOR (n:Symbol) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT v2_endpoint_id IF NOT EXISTS FOR (n:ApiEndpoint) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT v2_spec_id IF NOT EXISTS FOR (n:ApiSpec) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT v2_client_id IF NOT EXISTS FOR (n:ApiClient) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT v2_external_id IF NOT EXISTS FOR (n:ExternalApi) REQUIRE n.id IS UNIQUE",
    "CREATE INDEX v2_endpoint_path IF NOT EXISTS FOR (n:ApiEndpoint) ON (n.path)",
    "CREATE INDEX v2_symbol_name IF NOT EXISTS FOR (n:Symbol) ON (n.name)",
    "CREATE FULLTEXT INDEX v2_search IF NOT EXISTS FOR (n:ApiEndpoint|Symbol|Service) "
    "ON EACH [n.name, n.qualified, n.path]",
]


class Neo4jStore:
    """Persists snapshots to Neo4j; loads hydrate a GraphSnapshot.

    The hydrated snapshot is cached on the instance (the API calls
    load() per request) and invalidated on every save.
    """

    def __init__(self) -> None:
        self._cached: GraphSnapshot | None = None
        self._load_lock = threading.Lock()

    def _session(self) -> Any:
        from axon.db.connection import sync_session

        return sync_session()

    def ensure_schema(self) -> None:
        with self._session() as session:
            for stmt in V2_CONSTRAINTS:
                session.run(stmt)

    @staticmethod
    def _write_chunk(session: Any, query: str, rows: list[dict[str, Any]]) -> None:
        """Run one batch in a managed write transaction so the driver
        retries transient errors (including OOM pressure). Plain
        session.run auto-commits are not retried."""
        execute_write = getattr(session, "execute_write", None)
        if execute_write is None:
            session.run(query, rows=rows)
            return

        def _tx(tx: Any) -> None:
            tx.run(query, rows=rows)

        execute_write(_tx)

    def save(self, snap: GraphSnapshot) -> None:
        repos = sorted({n.repo_id for n in snap.nodes.values() if n.repo_id})
        with self._session() as session:
            for repo_id in repos:
                # IN TRANSACTIONS commits in sub-batches inside one
                # auto-commit statement, so deleting a huge repo does
                # not build one heap-bound transaction. It cannot run
                # inside execute_write, hence session.run here.
                session.run(
                    "MATCH (n {repo_id: $repo_id}) "
                    "CALL { WITH n DETACH DELETE n } "
                    f"IN TRANSACTIONS OF {NEO4J_DELETE_BATCH_SIZE} ROWS",
                    repo_id=repo_id,
                )
            nodes_by_label: dict[str, list[GraphNode]] = {}
            for n in snap.nodes.values():
                nodes_by_label.setdefault(n.kind.value, []).append(n)
            for label, nodes in nodes_by_label.items():
                for node_chunk in _chunks(nodes, NEO4J_BATCH_SIZE):
                    node_rows = [
                        {
                            "id": n.id,
                            "props": {
                                "id": n.id,
                                "name": n.name,
                                "repo_id": n.repo_id,
                                "service_id": n.service_id,
                                "file": n.file,
                                "line_start": n.line_start,
                                "line_end": n.line_end,
                                **n.props,
                            },
                        }
                        for n in node_chunk
                    ]
                    # Label comes from the NodeKind enum only; ids/props are parameters.
                    self._write_chunk(
                        session,
                        f"UNWIND $rows AS row MERGE (x:{label} {{id: row.id}}) SET x = row.props",
                        node_rows,
                    )
            # Group edges by endpoint labels so MATCH can use the
            # per-label unique id constraints instead of label scans.
            # Endpoints outside this snapshot (other repos) fall back
            # to unlabelled patterns for that side.
            label_of = {n.id: n.kind.value for n in snap.nodes.values()}
            edges_by_shape: dict[tuple[str, str, str], list[GraphEdge]] = {}
            for e in snap.edges:
                key = (label_of.get(e.from_id, ""), label_of.get(e.to_id, ""), e.kind.value)
                edges_by_shape.setdefault(key, []).append(e)
            for (from_label, to_label, kind), edges in edges_by_shape.items():
                a_pat = f"(a:{from_label} {{id: row.a}})" if from_label else "(a {id: row.a})"
                b_pat = f"(b:{to_label} {{id: row.b}})" if to_label else "(b {id: row.b})"
                for edge_chunk in _chunks(edges, NEO4J_BATCH_SIZE):
                    edge_rows = [
                        {
                            "a": e.from_id,
                            "b": e.to_id,
                            "conf": e.confidence.value,
                            "file": e.evidence.file,
                            "line": e.evidence.line,
                            "rule": e.evidence.rule,
                        }
                        for e in edge_chunk
                    ]
                    self._write_chunk(
                        session,
                        "UNWIND $rows AS row "
                        f"MATCH {a_pat}, {b_pat} "
                        f"MERGE (a)-[r:{kind}]->(b) "
                        "SET r.confidence = row.conf, r.file = row.file, "
                        "r.line = row.line, r.rule = row.rule",
                        edge_rows,
                    )
        with self._load_lock:
            if self._cached is not None:
                # Mirror the DB write into the cached snapshot instead
                # of forcing a full rehydration on the next request.
                MemoryStore(self._cached).save(snap)
        logger.info("saved %d nodes, %d edges to Neo4j", len(snap.nodes), len(snap.edges))

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
        from axon.v2.models import Evidence

        snap = GraphSnapshot()
        scalar = {"id", "name", "repo_id", "service_id", "file", "line_start", "line_end"}
        with self._session() as session:
            # Iterate the cursor record-by-record: .data() would first
            # materialize millions of row dicts on top of the snapshot
            # being built, roughly doubling peak memory.
            for rec in session.run("MATCH (n) RETURN labels(n) AS labels, properties(n) AS props"):
                props = dict(rec["props"])
                extra = {k: str(v) for k, v in props.items() if k not in scalar}
                snap.add_node(
                    GraphNode(
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
                )
            for rec in session.run(
                "MATCH (a)-[r]->(b) RETURN a.id AS a, b.id AS b, "
                "type(r) AS kind, properties(r) AS props"
            ):
                p = rec["props"]
                snap.add_edge(
                    GraphEdge(
                        from_id=rec["a"],
                        to_id=rec["b"],
                        kind=rec["kind"],
                        confidence=p.get("confidence", "EXTRACTED"),
                        evidence=Evidence(
                            file=p.get("file", ""),
                            line=int(p.get("line", 0) or 0),
                            rule=p.get("rule", ""),
                        ),
                    )
                )
        return snap

    def run_cypher(self, cypher: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self._session() as session:
            for rec in session.run(cypher):
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
    """`axon-init`: create v2 Neo4j constraints/indexes (idempotent)."""
    import logging as _logging

    _logging.basicConfig(level="INFO")
    Neo4jStore().ensure_schema()
    print("Axon v2 Neo4j schema ready.")


def make_store(backend: str) -> GraphStore:
    if backend == "neo4j":
        store = Neo4jStore()
        try:
            store.ensure_schema()
        except Exception as exc:
            logger.warning("Neo4j unavailable (%s); falling back to memory", exc)
            return MemoryStore()
        return store
    return MemoryStore()
