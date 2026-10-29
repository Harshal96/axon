"""
Graph traversal operations:
  - dependency_path: shortest DEPENDS_ON path between two targets
  - callers / callees: CALLS edge traversal
  - subgraph: multi-root BFS expansion for Claude context
  - affected_tests: reverse traversal from changed files to test targets
"""

from __future__ import annotations

import json
import logging

from axon.db.connection import async_session
from axon.graph.envelope import timed_operation
from axon.graph.models import (
    AffectedTestsResult,
    ApiContract,
    CallEntry,
    CallGraphResult,
    DependencyPathResult,
    GeneratedCodeRef,
    GraphMeta,
    KeySymbol,
    SubgraphEdge,
    SubgraphResult,
    SubgraphTarget,
    TestTarget,
    Warning,
)

logger = logging.getLogger(__name__)


async def dependency_path(from_label: str, to_label: str) -> DependencyPathResult:
    """Finds the shortest DEPENDS_ON path between two BazelTargets."""

    async def _run(meta: GraphMeta) -> DependencyPathResult:
        async with async_session() as session:
            result = await session.run(
                """
                MATCH (from:BazelTarget {label: $from_label})
                MATCH (to:BazelTarget {label: $to_label})
                MATCH path = shortestPath((from)-[:DEPENDS_ON*..10]->(to))
                RETURN [n IN nodes(path) | n.label] AS path_labels,
                       length(path) AS hops
            """,
                from_label=from_label,
                to_label=to_label,
            )
            rec = await result.single()

        if rec is None:
            return DependencyPathResult(
                meta=meta,
                found=False,
                from_label=from_label,
                to_label=to_label,
                warnings=[
                    Warning(
                        code="NO_PATH",
                        message=f"No dependency path from {from_label} to {to_label}",
                    )
                ],
            )

        meta.nodes_traversed += rec["hops"] + 1

        return DependencyPathResult(
            meta=meta,
            found=True,
            from_label=from_label,
            to_label=to_label,
            hops=rec["hops"],
            paths=[rec["path_labels"]],
        )

    return await timed_operation(10, _run)


async def callers(symbol_id: str, depth: int = 1) -> CallGraphResult:
    """Find symbols that call the given symbol (up to depth hops)."""
    depth = min(depth, 5)

    async def _run(meta: GraphMeta) -> CallGraphResult:
        async with async_session() as session:
            result = await session.run(
                f"""
                MATCH (target:Symbol {{id: $symbol_id}})
                OPTIONAL MATCH path = (caller:Symbol)-[:CALLS*1..{depth}]->(target)
                WHERE caller IS NOT NULL
                OPTIONAL MATCH (f:SourceFile)-[:DEFINES]->(caller)
                RETURN caller, length(path) AS call_depth, f.path AS file_path
                ORDER BY call_depth
            """,
                symbol_id=symbol_id,
            )
            records = await result.data()

        entries = [
            CallEntry(
                id=r["caller"].get("id"),
                name=r["caller"].get("name"),
                qualified_name=r["caller"].get("qualified_name"),
                kind=r["caller"].get("kind"),
                depth=r.get("call_depth", 1),
                file_path=r.get("file_path"),
            )
            for r in records
            if r.get("caller")
        ]

        meta.nodes_traversed += len(records)

        return CallGraphResult(
            meta=meta,
            symbol_id=symbol_id,
            depth=depth,
            direction="CALLERS",
            entries=entries,
            total=len(entries),
        )

    return await timed_operation(depth, _run)


async def callees(symbol_id: str, depth: int = 3) -> CallGraphResult:
    """Find symbols called by the given symbol (up to depth hops)."""
    depth = min(depth, 7)

    async def _run(meta: GraphMeta) -> CallGraphResult:
        async with async_session() as session:
            result = await session.run(
                f"""
                MATCH (source:Symbol {{id: $symbol_id}})
                OPTIONAL MATCH path = (source)-[:CALLS*1..{depth}]->(callee:Symbol)
                WHERE callee IS NOT NULL
                OPTIONAL MATCH (f:SourceFile)-[:DEFINES]->(callee)
                RETURN callee, length(path) AS call_depth, f.path AS file_path
                ORDER BY call_depth
            """,
                symbol_id=symbol_id,
            )
            records = await result.data()

        entries = [
            CallEntry(
                id=r["callee"].get("id"),
                name=r["callee"].get("name"),
                qualified_name=r["callee"].get("qualified_name"),
                kind=r["callee"].get("kind"),
                depth=r.get("call_depth", 1),
                file_path=r.get("file_path"),
            )
            for r in records
            if r.get("callee")
        ]

        meta.nodes_traversed += len(records)

        return CallGraphResult(
            meta=meta,
            symbol_id=symbol_id,
            depth=depth,
            direction="CALLEES",
            entries=entries,
            total=len(entries),
        )

    return await timed_operation(depth, _run)


def _estimate_tokens(obj: object) -> int:
    return len(json.dumps(obj, default=str)) // 4


_SUBGRAPH_TARGETS = """
UNWIND $labels AS lbl
MATCH (t:BazelTarget {label: lbl})
OPTIONAL MATCH (t)-[:BUILDS|PRODUCES]->(sf:SourceFile)
OPTIONAL MATCH (sf)-[:DEFINES]->(sym:Symbol)
WITH t,
     collect(DISTINCT sf.path)[..10] AS src_files,
     collect(DISTINCT {
         name: sym.name,
         kind: sym.kind,
         signature: sym.signature,
         docstring: sym.docstring,
         line: sym.line_start
     })[..8] AS symbols
RETURN t.label AS label, t.kind AS kind, t.team_owner AS team,
       src_files, symbols
"""

_SUBGRAPH_EDGES = """
UNWIND $labels AS lbl
MATCH (t:BazelTarget {label: lbl})-[r:DEPENDS_ON]->(dep:BazelTarget)
WHERE dep.label IN $labels
RETURN t.label AS from_label, dep.label AS to_label,
       type(r) AS edge_type, r.dep_type AS dep_type
"""

_SUBGRAPH_DEPTH_EXPANSION = """
UNWIND $labels AS lbl
MATCH (t:BazelTarget {label: lbl})-[:DEPENDS_ON*1..$depth]->(dep:BazelTarget)
RETURN DISTINCT dep.label AS label
LIMIT 50
"""

_SWAGGER_FOR_TARGETS = """
UNWIND $labels AS lbl
MATCH (t:BazelTarget {label: lbl})-[:EXPOSES_API]->(ep:ApiEndpoint)
MATCH (sw:SwaggerSchema)-[:DEFINES_ENDPOINT]->(ep)
WITH sw, collect(ep.http_method + ' ' + ep.path) AS endpoints
RETURN sw.path AS swagger_path, endpoints, sw.content_hash AS schema_hash
"""

_GENERATED_CLIENTS = """
UNWIND $labels AS lbl
MATCH (t:BazelTarget {label: lbl})
WHERE t.has_swagger_src = true
RETURN t.label AS gen_label, t.genrule_srcs AS srcs
"""


async def subgraph(
    labels: list[str],
    depth: int = 2,
    token_budget: int = 4000,
) -> SubgraphResult:
    """Extract a minimal subgraph for a list of BazelTarget roots within a token budget."""
    depth = min(depth, 4)

    async def _run(meta: GraphMeta) -> SubgraphResult:
        nonlocal labels

        if depth > 1:
            async with async_session() as session:
                exp = await session.run(_SUBGRAPH_DEPTH_EXPANSION, labels=labels, depth=depth)
                expanded = await exp.data()
                extra = [r["label"] for r in expanded]
                labels = list(dict.fromkeys(labels + extra))  # preserve order, dedupe

        async with async_session() as session:
            target_result = await session.run(_SUBGRAPH_TARGETS, labels=labels)
            target_records = await target_result.data()

            edge_result = await session.run(_SUBGRAPH_EDGES, labels=labels)
            edge_records = await edge_result.data()

            swagger_result = await session.run(_SWAGGER_FOR_TARGETS, labels=labels)
            swagger_records = await swagger_result.data()

            gen_result = await session.run(_GENERATED_CLIENTS, labels=labels)
            gen_records = await gen_result.data()

        targets: list[SubgraphTarget] = []
        tokens_used = 0

        for rec in target_records:
            symbols = [
                KeySymbol(
                    name=s["name"] or "",
                    kind=s["kind"] or "",
                    signature=s.get("signature"),
                    docstring=s.get("docstring"),
                    line=s.get("line") or 0,
                )
                for s in rec["symbols"]
                if s.get("name")
            ]
            t = SubgraphTarget(
                label=rec["label"],
                kind=rec["kind"] or "UNKNOWN",
                team=rec["team"],
                source_files=rec["src_files"],
                key_symbols=symbols,
            )
            t_tokens = _estimate_tokens(t.model_dump())
            if tokens_used + t_tokens > token_budget:
                break
            targets.append(t)
            tokens_used += t_tokens

        edges = [
            SubgraphEdge(
                from_label=r["from_label"],
                to_label=r["to_label"],
                edge_type=r["edge_type"],
                depth=1,
            )
            for r in edge_records
        ]

        api_contracts = [
            ApiContract(
                swagger_path=r["swagger_path"],
                endpoints=r["endpoints"],
                schema_hash=r["schema_hash"],
            )
            for r in swagger_records
        ]

        generated_code = [
            GeneratedCodeRef(
                label=r["gen_label"],
                generated_from=r["srcs"][0] if r["srcs"] else "unknown",
                warning=f"DO NOT EDIT — regenerate with: bazel build {r['gen_label']}",
            )
            for r in gen_records
        ]

        service_count = len([t for t in targets if t.kind in ("PY_BINARY", "JAVA_BINARY")])
        lib_count = len([t for t in targets if "LIBRARY" in t.kind])
        api_count = sum(len(c.endpoints) for c in api_contracts)
        summary = (
            f"{len(targets)} targets ({service_count} services, {lib_count} libraries). "
            f"{api_count} API endpoints exposed. "
            f"{len(generated_code)} generated client(s). "
            f"{len(edges)} dependency edges."
        )

        meta.nodes_traversed += len(target_records) + len(edge_records)

        return SubgraphResult(
            meta=meta,
            architecture_summary=summary,
            scope={
                "roots": labels[: len(labels)],
                "depth": depth,
                "included_targets": len(targets),
            },
            targets=targets,
            edges=edges,
            api_contracts=api_contracts,
            generated_code=generated_code,
            token_estimate=tokens_used,
        )

    return await timed_operation(depth, _run)


_FILES_TO_TARGETS = """
UNWIND $file_paths AS fp
MATCH (sf:SourceFile {path: fp})<-[:BUILDS|PRODUCES]-(t:BazelTarget)
RETURN DISTINCT t.label AS label, fp AS via_file
"""

_RDEPS_TESTS = """
UNWIND $root_labels AS root
MATCH (root_target:BazelTarget {label: root})
CALL apoc.path.expandConfig(root_target, {
    relationshipFilter: '<DEPENDS_ON',
    minLevel: 1,
    maxLevel: 10,
    uniqueness: 'NODE_GLOBAL',
    labelFilter: '+BazelTarget'
}) YIELD path
WITH last(nodes(path)) AS dep, root
WHERE dep.testonly = true
  AND dep.kind IN ['PY_TEST', 'JAVA_TEST', 'TEST_SUITE', 'JS_TEST']
RETURN DISTINCT dep.label AS label, dep.kind AS kind,
               dep.team_owner AS team, root AS via_target
"""


async def affected_tests(changed: list[str]) -> AffectedTestsResult:
    """
    Given a list of changed file paths or Bazel target labels, returns all
    test targets that should be run.
    """

    async def _run(meta: GraphMeta) -> AffectedTestsResult:
        file_paths = [c for c in changed if not c.startswith("//")]
        direct_labels = [c for c in changed if c.startswith("//")]

        async with async_session() as session:
            if file_paths:
                result = await session.run(_FILES_TO_TARGETS, file_paths=file_paths)
                records = await result.data()
                direct_labels += [r["label"] for r in records]

            direct_labels = list(dict.fromkeys(direct_labels))

            if not direct_labels:
                return AffectedTestsResult(
                    meta=meta,
                    changed_files=changed,
                    affected_test_targets=[],
                    total=0,
                    by_team={},
                )

            test_result = await session.run(_RDEPS_TESTS, root_labels=direct_labels)
            test_records = await test_result.data()

        test_targets: list[TestTarget] = []
        seen: set[str] = set()
        by_team: dict[str, int] = {}

        for rec in test_records:
            if rec["label"] in seen:
                continue
            seen.add(rec["label"])
            team = rec.get("team")
            if team:
                by_team[team] = by_team.get(team, 0) + 1
            test_targets.append(
                TestTarget(
                    label=rec["label"],
                    kind=rec["kind"],
                    team=team,
                    reached_via=rec["via_target"],
                )
            )

        meta.nodes_traversed += len(test_records)

        return AffectedTestsResult(
            meta=meta,
            changed_files=changed,
            affected_test_targets=test_targets,
            total=len(test_targets),
            by_team=by_team,
        )

    return await timed_operation(10, _run)
