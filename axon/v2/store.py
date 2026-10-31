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
from axon.v2.graph_delta import GraphDelta, edge_key, replace_repositories
from axon.v2.models import GraphEdge, GraphNode

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


class DeltaNeedsFullSave(RuntimeError):
    """Legacy relationships need one full repository save before delta writes."""


class Neo4jStore:
    """Persists snapshots to Neo4j; loads hydrate a GraphSnapshot.

    The hydrated snapshot is cached on the instance (the API calls
    load() per request) and atomically replaced after successful saves.
    """

    def __init__(self) -> None:
        self._cached: GraphSnapshot | None = None
        self._load_lock = threading.Lock()
        self.last_save_s = 0.0

    def _session(self) -> Any:
        from axon.db.connection import sync_session

        return sync_session()

    def ensure_schema(self) -> None:
        with self._session() as session:
            for stmt in V2_CONSTRAINTS:
                session.run(stmt)

    @staticmethod
    def _write_rows(
        tx: Any, nodes_input: dict[str, GraphNode], edges_input: list[GraphEdge]
    ) -> None:
        nodes_by_label: dict[str, list[GraphNode]] = {}
        for n in nodes_input.values():
            nodes_by_label.setdefault(n.kind.value, []).append(n)
        for label, nodes in nodes_by_label.items():
            for node_chunk in _chunks(nodes, NEO4J_BATCH_SIZE):
                node_rows = [
                    {
                        "id": n.id,
                        "props": {
                            **n.props,
                            "id": n.id,
                            "name": n.name,
                            "repo_id": n.repo_id,
                            "service_id": n.service_id,
                            "file": n.file,
                            "line_start": n.line_start,
                            "line_end": n.line_end,
                            "_axon_props": json.dumps(n.props),
                        },
                    }
                    for n in node_chunk
                ]
                # Label comes from the NodeKind enum only; ids/props are parameters.
                tx.run(
                    f"UNWIND $rows AS row MERGE (x:{label} {{id: row.id}}) SET x = row.props",
                    rows=node_rows,
                )
        # Group edges by endpoint labels so MATCH can use the
        # per-label unique id constraints instead of label scans.
        # Endpoints outside this snapshot (other repos) fall back
        # to unlabelled patterns for that side.
        label_of = {n.id: n.kind.value for n in nodes_input.values()}
        edges_by_shape: dict[tuple[str, str, str], list[GraphEdge]] = {}
        for e in edges_input:
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
                        "props": {
                            **e.props,
                            "confidence": e.confidence.value,
                            **e.evidence.model_dump(),
                            "_axon_props": json.dumps(e.props),
                            "_axon_key": edge_key(e),
                        },
                    }
                    for e in edge_chunk
                ]
                tx.run(
                    "UNWIND $rows AS row "
                    f"MATCH {a_pat}, {b_pat} "
                    f"CREATE (a)-[r:{kind}]->(b) "
                    "SET r = row.props",
                    rows=edge_rows,
                )

    def save(self, snap: GraphSnapshot) -> None:
        started = time.perf_counter()
        repos = sorted({n.repo_id for n in snap.nodes.values() if n.repo_id})

        def write(tx: Any) -> None:
            for repo_id in repos:
                tx.run("MATCH (n {repo_id: $repo_id}) DETACH DELETE n", repo_id=repo_id)
            self._write_rows(tx, snap.nodes, snap.edges)
            tx.run(
                "MATCH (n:ExternalApi) WHERE coalesce(n.repo_id, '') = '' AND NOT (n)--() DELETE n"
            )

        # Existing logical-id constraints and unrestricted read-only Cypher require
        # one atomic transaction rather than hidden shadow generations. Batching
        # bounds request payloads, not the transaction's server-side memory.
        with self._load_lock:
            candidate = (
                replace_repositories(self._cached, snap) if self._cached is not None else None
            )
            with self._session() as session:
                session.execute_write(write)
            self._cached = candidate
        self.last_save_s = time.perf_counter() - started
        logger.info(
            "saved %d nodes, %d edges to Neo4j in %.3fs",
            len(snap.nodes),
            len(snap.edges),
            self.last_save_s,
        )

    def apply_delta(self, delta: GraphDelta) -> None:
        """Write only changed rows, atomically, without hydrating a warm cache.

        Raises DeltaNeedsFullSave before writes when affected legacy relationships
        lack complete edge keys. The caller must publish a full repo snapshot.
        """
        started = time.perf_counter()
        affected_ids = set(delta.upsert_nodes) | delta.remove_node_ids
        for key in delta.remove_edge_keys:
            data = json.loads(key)
            affected_ids.update((data["from_id"], data["to_id"]))
        for edge in delta.add_edges:
            affected_ids.update((edge.from_id, edge.to_id))

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
            for keys in _chunks(sorted(delta.remove_edge_keys), NEO4J_BATCH_SIZE):
                tx.run("MATCH ()-[r]->() WHERE r._axon_key IN $keys DELETE r", keys=keys)
            for ids in _chunks(sorted(delta.remove_node_ids), NEO4J_BATCH_SIZE):
                tx.run("MATCH (n) WHERE n.id IN $ids DETACH DELETE n", ids=ids)
            self._write_rows(tx, delta.upsert_nodes, delta.add_edges)

        with self._load_lock:
            candidate = delta.apply(self._cached) if self._cached is not None else None
            with self._session() as session:
                session.execute_write(write)
            self._cached = candidate
        self.last_save_s = time.perf_counter() - started
        logger.info(
            "applied graph delta: %d node upserts, %d removals, %d edge additions in %.3fs",
            len(delta.upsert_nodes),
            len(delta.remove_node_ids),
            len(delta.add_edges),
            self.last_save_s,
        )

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
                extra = (
                    json.loads(props["_axon_props"])
                    if "_axon_props" in props
                    else {k: str(v) for k, v in props.items() if k not in scalar}
                )
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
                            detail=p.get("detail", ""),
                        ),
                        props=json.loads(p.get("_axon_props", "{}")),
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
