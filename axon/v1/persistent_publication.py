"""Bounded Neo4j publication producer. Policy is supplied by MemoryStore.

All staged objects are invisible until the final source/input/link marker switch.
Only one writer process per database is supported.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from axon.v1.graph import GraphSnapshot
from axon.v1.link_inputs import PublicationStats, RepoLinkReport
from axon.v1.link_publication import encode_link_inputs, encode_link_reports
from axon.v1.models import GRAPH_SCHEMA_VERSION, EdgeKind, NodeKind
from axon.v1.repository_links import _legacy_inputs

if TYPE_CHECKING:
    from axon.v1.store import Neo4jStore

logger = logging.getLogger(__name__)


def persist_candidate(
    store: Neo4jStore,
    current: GraphSnapshot,
    candidate: GraphSnapshot,
    *,
    started: float,
    reconcile_s: float,
) -> GraphSnapshot:
    """Stage complete replacements and projection, then publish in one transaction."""
    from axon.v1.store import NEO4J_BATCH_SIZE, _chunks

    for edge in candidate.edges:
        if edge.from_id not in candidate.nodes or edge.to_id not in candidate.nodes:
            raise ValueError(
                f"prospective graph has dangling {edge.kind}: {edge.from_id} -> {edge.to_id}"
            )
    stats = PublicationStats(
        replayed_repositories=len(candidate.link_inputs),
        replayed_sites=sum(len(i.prepared) + len(i.pinned) for i in candidate.link_inputs.values()),
        reconcile_s=reconcile_s,
    )
    pubs = {str(p["repo_id"]): p for p in store.stored_publications()}
    old_repos = {n.repo_id for n in current.nodes.values() if n.kind == NodeKind.REPO}
    new_repos = set(candidate.link_inputs)
    physical = dict(store._physical_ids)
    generations = dict(store._generations)
    input_rows: list[dict[str, Any]] = []
    node_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    edge_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    markers: list[dict[str, Any]] = []
    baseline: dict[str, tuple[str, str]] = {}

    def structure(snapshot: GraphSnapshot, repo: str, generation: str) -> None:
        local = {
            n.id: f"{generation}:{n.id}"
            for n in snapshot.nodes.values()
            if n.repo_id == repo and n.kind != NodeKind.EXTERNAL_API
        }
        physical.update(local)
        for node_id, storage_id in local.items():
            node = snapshot.nodes[node_id]
            node_groups[node.kind.value].append(store._node_row(node, storage_id, repo, generation))
            stats.structural_nodes += 1
        for edge in snapshot.edges:
            if edge.kind != EdgeKind.CALLS_API and edge.from_id in local:
                # Structure is repository owned. Cross-repository outbound uses CALLS_API.
                if edge.to_id not in local:
                    raise ValueError("structural edges must remain within their repository")
                shape = (
                    snapshot.nodes[edge.from_id].kind.value,
                    snapshot.nodes[edge.to_id].kind.value,
                    edge.kind.value,
                )
                edge_groups[shape].append(store._edge_row(edge, local))
                stats.structural_edges += 1

    # Retain the old source/input pair even on the first legacy activation.
    # Bootstrap metadata never becomes visible until its marker pointer commits.
    for repo in sorted(old_repos):
        previous = pubs.get(repo, {})
        generation = str(previous.get("generation") or generations.get(repo) or "")
        nodes = [
            n
            for n in current.nodes.values()
            if n.repo_id == repo and n.kind != NodeKind.EXTERNAL_API
        ]
        if not generation or any(n.id not in physical for n in nodes):
            generation = uuid.uuid4().hex
            structure(current, repo, generation)
        input_generation = str(previous.get("input_generation") or "")
        if not input_generation:
            input_generation = uuid.uuid4().hex
            inputs = deepcopy(current.link_inputs.get(repo) or _legacy_inputs(current, repo))
            inputs.source_generation = generation
            input_rows.extend(
                encode_link_inputs({repo: inputs}, owner=repo, generation=input_generation)
            )
        baseline[repo] = generation, input_generation

    for repo in sorted(old_repos | new_repos | set(pubs)):
        previous = pubs.get(repo, {})
        old_gen, old_input = baseline.get(
            repo,
            (str(previous.get("generation") or ""), str(previous.get("input_generation") or "")),
        )
        if repo not in new_repos:
            # Tombstones prevent unversioned legacy rows from becoming visible again.
            if repo not in old_repos and previous.get("deleted"):
                generation, input_generation = old_gen, old_input
            else:
                generation, input_generation = uuid.uuid4().hex, ""
        else:
            inputs = candidate.link_inputs[repo]
            old_inputs = current.link_inputs.get(repo)
            changed = old_inputs is None or inputs.source_generation != old_inputs.source_generation
            if repo in old_repos and old_inputs is None:
                # Reconciliation bootstrapped retained legacy sources; only an explicitly
                # replaced source receives a nonempty new generation from MemoryStore.
                changed = bool(inputs.source_generation)
            if changed or repo not in old_repos:
                generation = inputs.source_generation or uuid.uuid4().hex
                inputs.source_generation = generation
                structure(candidate, repo, generation)
                input_generation = generation
                input_rows.extend(
                    encode_link_inputs({repo: inputs}, owner=repo, generation=input_generation)
                )
            else:
                generation, input_generation = old_gen, old_input
                inputs.source_generation = generation
                if old_inputs is not None and inputs != old_inputs:
                    # Defaulted metadata ownership may be adopted during a
                    # target-only replay. Persist it without rewriting source
                    # rows or replacing the retained source/input rollback pair.
                    input_generation = uuid.uuid4().hex
                    input_rows.extend(
                        encode_link_inputs({repo: inputs}, owner=repo, generation=input_generation)
                    )
        generations[repo] = generation
        replacing = generation != old_gen
        previous_generation = old_gen if replacing else previous.get("prev_generation")
        previous_input = old_input if replacing else previous.get("prev_input_generation")
        if (
            previous_generation
            and not previous_input
            and int(previous.get("schema_version") or 1) < 2
        ):
            # Schema-1 may already retain a usable previous physical source.
            # Public hydration cannot recover its links after global activation.
            retained = store._legacy_generation_inputs(repo, str(previous_generation))
            previous_input = uuid.uuid4().hex
            input_rows.extend(
                encode_link_inputs({repo: retained}, owner=repo, generation=previous_input)
            )
        markers.append(
            {
                "repo_id": repo,
                "generation": generation,
                "input_generation": input_generation or None,
                "prev_generation": previous_generation,
                "prev_input_generation": previous_input,
                "schema_version": GRAPH_SCHEMA_VERSION,
                "deleted": repo not in new_repos,
            }
        )

    link_generation = candidate.publication_id
    for node in candidate.nodes.values():
        if node.kind == NodeKind.EXTERNAL_API:
            physical[node.id] = f"{link_generation}:external:{node.id}"
            row = store._node_row(node, physical[node.id], "", "")
            row["props"]["_axon_link_generation"] = link_generation
            node_groups[node.kind.value].append(row)
            stats.link_nodes += 1
    for edge in candidate.edges:
        if edge.kind == EdgeKind.CALLS_API:
            row = store._edge_row(edge, physical)
            row["props"]["_axon_link_generation"] = link_generation
            shape = (
                candidate.nodes[edge.from_id].kind.value,
                candidate.nodes[edge.to_id].kind.value,
                edge.kind.value,
            )
            edge_groups[shape].append(row)
            stats.link_edges += 1
    reports = encode_link_reports(
        candidate.link_reports, owner="global", generation=link_generation
    )
    snapshot_reports = encode_link_reports(
        {
            "snapshot": RepoLinkReport(
                unresolved=candidate.unresolved, diagnostics=candidate.diagnostics
            )
        },
        owner="global",
        generation=link_generation,
    )
    stats.input_rows, stats.report_rows = len(input_rows), len(reports) + len(snapshot_reports)
    stats.staged_rows = (
        stats.structural_nodes
        + stats.structural_edges
        + stats.link_nodes
        + stats.link_edges
        + len(input_rows)
        + len(reports)
        + len(snapshot_reports)
    )
    total = (
        sum(
            (len(rows) + NEO4J_BATCH_SIZE - 1) // NEO4J_BATCH_SIZE
            for rows in [
                *node_groups.values(),
                *edge_groups.values(),
                input_rows,
                reports,
                snapshot_reports,
            ]
        )
        + 1
    )
    done = 0

    def progress() -> None:
        if store.publish_progress:
            store.publish_progress(done, total)

    staging_started = time.perf_counter()
    with store._session() as session:
        store._preflight(session, candidate)
        for label, rows in node_groups.items():
            for chunk in _chunks(rows, NEO4J_BATCH_SIZE):
                store._run_batch(
                    session,
                    f"UNWIND $rows AS row CREATE (x:{label}) SET x = row.props RETURN "
                    f"count(x) AS written",
                    label=label,
                    rows=chunk,
                )
                done += 1
                progress()
        for (a_label, b_label, kind), rows in edge_groups.items():
            for chunk in _chunks(rows, NEO4J_BATCH_SIZE):
                store._run_batch(
                    session,
                    f"UNWIND $rows AS row MATCH (a:{a_label} {{_axon_storage_id: row.a}}), "
                    f"(b:{b_label} {{_axon_storage_id: row.b}}) "
                    f"CREATE (a)-[r:{kind}]->(b) SET r = row.props RETURN count(r) AS written",
                    label=kind,
                    rows=chunk,
                )
                done += 1
                progress()
        for label, rows in [
            ("_AxonLinkInput", input_rows),
            ("_AxonLinkReport", reports),
            ("_AxonSnapshotReport", snapshot_reports),
        ]:
            for chunk in _chunks(rows, NEO4J_BATCH_SIZE):
                store._run_batch(
                    session,
                    f"UNWIND $rows AS row CREATE (x:{label}) SET x = row RETURN count(x) AS "
                    f"written",
                    label=label,
                    rows=chunk,
                )
                done += 1
                progress()
        stats.staging_batches, stats.staging_s = done, time.perf_counter() - staging_started
        commit_started = time.perf_counter()
        # One managed transaction, including every repository's structural and input
        # pointers and the global link/report marker. The outer singleton row also
        # handles an empty graph publication.
        store._run_batch(
            session,
            "UNWIND $rows AS publication CALL { WITH publication "
            "UNWIND publication.markers AS row MERGE (p:_AxonPublication {repo_id: row.repo_id}) "
            "SET p.generation = row.generation, p.prev_generation = row.prev_generation, "
            "p.input_generation = row.input_generation, p.prev_input_generation = "
            "row.prev_input_generation, "
            "p.schema_version = row.schema_version, p.deleted = row.deleted RETURN count(p) "
            "AS markers } "
            "MERGE (link:_AxonLinkPublication {id: 'global'}) "
            "SET link.prev_generation = link.generation, link.generation = publication.generation, "
            "link.schema_version = publication.schema_version, link.commit = publication.commit, "
            "link.snapshot_report = true, link.report_repos = publication.report_repos "
            "REMOVE link.unresolved, link.diagnostics",
            label="publication",
            rows=[
                {
                    "markers": markers,
                    "generation": link_generation,
                    "schema_version": GRAPH_SCHEMA_VERSION,
                    "commit": candidate.commit,
                    "report_repos": sorted(candidate.link_reports),
                }
            ],
        )
        stats.commit_s = time.perf_counter() - commit_started
        # Everything below this point is post-commit. Never report it as an uncommitted
        # publication, including cancellation raised by a UI progress callback.
        cleanup_started = time.perf_counter()
        try:
            cleanup(store, session)
        except BaseException:
            logger.warning("Published graph; deferred obsolete-row cleanup", exc_info=True)
        stats.cleanup_s = time.perf_counter() - cleanup_started
        stats.total_s = time.perf_counter() - started
        candidate.publication_stats = stats
        store._cached = candidate
        store._physical_ids = {
            key: value for key, value in physical.items() if key in candidate.nodes
        }
        store._generations = generations
        store.last_save_s = stats.total_s
        done = total
        try:
            progress()
        except BaseException:
            logger.warning("Published graph; final progress callback failed", exc_info=True)
    return candidate


def cleanup(store: Neo4jStore, session: Any) -> None:
    """Bounded garbage collection retaining source/input pairs and two projections."""
    from axon.v1.store import NEO4J_BATCH_SIZE

    structural_obsolete = (
        "NOT EXISTS { MATCH (p:_AxonPublication) WHERE p.repo_id = "
        "coalesce(n._axon_owner,n.repo_id) "
        "AND n._axon_generation IN [p.generation,p.prev_generation] }"
    )
    # Delete relationships before nodes: a small node batch can have huge degree.
    for kind in NodeKind:
        if kind == NodeKind.EXTERNAL_API:
            continue
        predicate = structural_obsolete
        for query in (
            f"MATCH (n:{kind.value})-[r]-() WHERE {predicate} WITH DISTINCT r LIMIT $batch "
            f"DELETE r RETURN count(*) AS removed",
            f"MATCH (n:{kind.value}) WHERE {predicate} AND NOT (n)--() WITH n LIMIT $batch "
            f"DELETE n RETURN count(*) AS removed",
        ):
            _drain(session, query, NEO4J_BATCH_SIZE)
    for label, predicate in [
        (
            "_AxonLinkInput",
            "NOT EXISTS { MATCH (p:_AxonPublication) WHERE p.repo_id = n.owner AND "
            "n.generation IN [p.input_generation,p.prev_input_generation] }",
        ),
        (
            "_AxonLinkReport",
            "NOT EXISTS { MATCH (p:_AxonLinkPublication {id:'global'}) WHERE n.generation IN "
            "[p.generation,p.prev_generation] }",
        ),
        (
            "_AxonSnapshotReport",
            "NOT EXISTS { MATCH (p:_AxonLinkPublication {id:'global'}) "
            "WHERE n.generation IN [p.generation,p.prev_generation] }",
        ),
    ]:
        _drain(
            session,
            f"MATCH (n:{label}) WHERE {predicate} WITH n LIMIT $batch DELETE n RETURN "
            f"count(*) AS removed",
            NEO4J_BATCH_SIZE,
        )
    # Legacy CALLS_API can hold target metadata for retained source generations.
    # Keep those until their structural source is no longer retained above.
    link_obsolete = (
        "r._axon_link_generation IS NOT NULL AND NOT EXISTS { "
        "MATCH (p:_AxonLinkPublication {id:'global'}) "
        "WHERE r._axon_link_generation IN [p.generation,p.prev_generation] }"
    )
    _drain(
        session,
        f"MATCH ()-[r:CALLS_API]->() WHERE {link_obsolete} WITH r LIMIT $batch DELETE r "
        f"RETURN count(*) AS removed",
        NEO4J_BATCH_SIZE,
    )
    _drain(
        session,
        "MATCH (n:ExternalApi) WHERE NOT (n)--() AND NOT EXISTS { MATCH "
        "(p:_AxonLinkPublication {id:'global'}) WHERE n._axon_link_generation IN "
        "[p.generation,p.prev_generation] } WITH n LIMIT $batch DELETE n RETURN count(*) AS "
        "removed",
        NEO4J_BATCH_SIZE,
    )


def _drain(session: Any, query: str, batch: int) -> None:
    while True:
        rows = session.execute_write(lambda tx: list(tx.run(query, batch=batch)))
        if not rows or int(rows[0]["removed"]) < batch:
            return
