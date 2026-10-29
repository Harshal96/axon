"""
blast_radius: reverse DEPENDS_ON traversal from a BazelTarget or Symbol.
swagger_blast_radius: SwaggerSchema → GeneratedClient → consumer traversal.
"""

from __future__ import annotations

from axon.db.connection import async_session
from axon.graph.envelope import timed_operation
from axon.graph.models import (
    AffectedClient,
    AffectedConsumer,
    AffectedSymbol,
    AffectedTarget,
    BlastRadiusResult,
    CrossServiceApiRisk,
    GraphMeta,
    SwaggerBlastRadiusResult,
    Warning,
)

_BAZEL_BLAST_RADIUS = """
MATCH (root:BazelTarget {label: $label})
CALL apoc.path.expandConfig(root, {
    relationshipFilter: '<DEPENDS_ON',
    minLevel: 1,
    maxLevel: $depth,
    uniqueness: 'NODE_GLOBAL',
    labelFilter: '+BazelTarget'
}) YIELD path
WITH root, last(nodes(path)) AS affected, length(path) AS depth
MATCH (affected)-[:DEPENDS_ON*1..1]->(dep:BazelTarget)
WHERE dep.label = $label OR dep.label IN $already_found
WITH root, affected, depth,
     collect(DISTINCT dep.label) AS dep_labels,
     CASE WHEN depth = 1 THEN 'DIRECT' ELSE 'TRANSITIVE' END AS dep_type
RETURN
    affected.label        AS label,
    affected.kind         AS kind,
    affected.team_owner   AS team,
    affected.is_genrule   AS is_genrule,
    affected.has_swagger_src AS has_swagger,
    affected.testonly     AS testonly,
    depth,
    dep_type
ORDER BY depth ASC, affected.label ASC
LIMIT $limit
"""

_SYMBOL_CALLERS_FOR_TARGET = """
MATCH (caller_file:SourceFile)<-[:BUILDS]-(caller_target:BazelTarget {label: $affected_label})
MATCH (caller_file)-[:DEFINES]->(caller_sym:Symbol)
MATCH (caller_sym)-[c:CALLS]->(callee_sym:Symbol)
MATCH (callee_file:SourceFile)-[:DEFINES]->(callee_sym)
MATCH (callee_file)<-[:BUILDS|PRODUCES]-(root_target:BazelTarget {label: $root_label})
RETURN
    caller_sym.id         AS caller_id,
    caller_sym.name       AS caller_name,
    caller_file.path      AS caller_file,
    caller_sym.line_start AS caller_line,
    callee_sym.id         AS callee_id,
    callee_sym.name       AS callee_name,
    c.confidence          AS confidence
LIMIT 20
"""

_API_RISKS = """
MATCH (root:BazelTarget {label: $label})-[:EXPOSES_API]->(ep:ApiEndpoint)
MATCH (consumer:BazelTarget)-[:CONSUMES_API]->(ep)
WHERE consumer.label <> $label
RETURN
    ep.path         AS endpoint_path,
    ep.http_method  AS method,
    collect(DISTINCT consumer.label) AS consumers,
    ep.is_deprecated AS is_deprecated
"""


async def blast_radius(
    label: str | None = None,
    symbol_id: str | None = None,
    depth: int = 5,
    include_tests: bool = True,
) -> BlastRadiusResult:
    if not label and not symbol_id:
        raise ValueError("Either label or symbol_id must be provided")

    async def _run(meta: GraphMeta) -> BlastRadiusResult:
        async with async_session() as session:
            root_label = label
            if symbol_id and not label:
                res = await session.run(
                    """
                    MATCH (s:Symbol {id: $sid})<-[:DEFINES]-(f:SourceFile)<-[:BUILDS]-(t:BazelTarget)
                    RETURN t.label AS label LIMIT 1
                """,
                    sid=symbol_id,
                )
                rec = await res.single()
                root_label = rec["label"] if rec else None
                if not root_label:
                    return BlastRadiusResult(
                        meta=meta,
                        root_label=symbol_id or "",
                        root_kind="SYMBOL",
                        summary={},
                        affected_targets=[],
                        affected_by_team={},
                    )

            result = await session.run(
                _BAZEL_BLAST_RADIUS,
                label=root_label,
                depth=depth,
                already_found=[root_label],
                limit=500,
            )
            records = await result.data()

            affected_targets: list[AffectedTarget] = []
            generated_warnings: list[Warning] = []
            team_counts: dict[str, int] = {}
            nodes_traversed = len(records)

            for rec in records:
                if not include_tests and rec.get("testonly", False):
                    continue
                if not include_tests and rec.get("kind") in ("PY_TEST", "JAVA_TEST"):
                    continue

                affected_symbols: list[AffectedSymbol] = []
                if rec["depth"] <= 2:
                    sym_result = await session.run(
                        _SYMBOL_CALLERS_FOR_TARGET,
                        affected_label=rec["label"],
                        root_label=root_label,
                    )
                    sym_records = await sym_result.data()
                    for sr in sym_records:
                        affected_symbols.append(
                            AffectedSymbol(
                                symbol_id=sr["caller_id"],
                                name=sr["caller_name"],
                                kind="SYMBOL",
                                file_path=sr["caller_file"],
                                line=sr["caller_line"] or 0,
                                call_chain=[sr["caller_name"], sr["callee_name"]],
                            )
                        )
                    nodes_traversed += len(sym_records)

                if rec["dep_type"] == "DIRECT":
                    confidence = "EXTRACTED"
                    score = 0.95
                elif rec["depth"] <= 3:
                    confidence = "EXTRACTED"
                    score = 0.80
                else:
                    confidence = "INFERRED"
                    score = 0.60

                warning_msg: str | None = None
                if rec.get("is_genrule"):
                    warning_msg = "This is a generated target. Do not edit its outputs directly."
                    generated_warnings.append(
                        Warning(
                            code="GENERATED_CODE_IN_BLAST_RADIUS",
                            message=warning_msg,
                            affected_nodes=[rec["label"]],
                        )
                    )

                team = rec.get("team")
                if team:
                    team_counts[team] = team_counts.get(team, 0) + 1

                affected_targets.append(
                    AffectedTarget(
                        label=rec["label"],
                        kind=rec["kind"],
                        team=team,
                        depth=rec["depth"],
                        dependency_type=rec["dep_type"],
                        impact_confidence=confidence,
                        impact_score=score,
                        reason=f"{'DIRECT' if rec['dep_type'] == 'DIRECT' else 'TRANSITIVE'} Bazel dependency at depth {rec['depth']}",
                        is_generated=rec.get("is_genrule", False),
                        affected_symbols=affected_symbols,
                        warning=warning_msg,
                    )
                )

            api_result = await session.run(_API_RISKS, label=root_label)
            api_records = await api_result.data()
            api_risks: list[CrossServiceApiRisk] = []
            for ar in api_records:
                api_risks.append(
                    CrossServiceApiRisk(
                        endpoint=f"{ar['method']} {ar['endpoint_path']}",
                        consumers=ar["consumers"],
                        risk_level="HIGH" if ar["is_deprecated"] is False else "LOW",
                        reason="Changing this target affects a public API endpoint consumed by other services.",
                    )
                )

            # Fetch root kind and team
            root_kind, root_team = "", None
            root_res = await session.run(
                "MATCH (t:BazelTarget {label: $label}) RETURN t.kind AS kind, t.team_owner AS team",
                label=root_label,
            )
            root_rec = await root_res.single()
            if root_rec:
                root_kind = root_rec["kind"] or ""
                root_team = root_rec["team"]

        meta.nodes_traversed += nodes_traversed

        return BlastRadiusResult(
            meta=meta,
            root_label=root_label or "",
            root_kind=root_kind,
            root_team=root_team,
            summary={
                "total_affected_targets": len(affected_targets),
                "total_affected_teams": len(team_counts),
                "max_depth_used": depth,
                "breaking_api_changes": len([r for r in api_risks if r.risk_level == "HIGH"]),
            },
            affected_targets=affected_targets,
            affected_by_team=team_counts,
            cross_service_api_risks=api_risks,
            warnings=generated_warnings,
        )

    return await timed_operation(depth, _run)


async def swagger_blast_radius(swagger_path: str) -> SwaggerBlastRadiusResult:
    """
    Find GeneratedClients and consumer BazelTargets affected by a swagger spec.
    """

    async def _run(meta: GraphMeta) -> SwaggerBlastRadiusResult:
        async with async_session() as session:
            result = await session.run(
                """
                MATCH (s:SwaggerSchema {path: $path})
                OPTIONAL MATCH (s)-[:DEFINES_ENDPOINT]->(e:ApiEndpoint)
                OPTIONAL MATCH (gc:GeneratedClient)-[:GENERATED_FROM]->(s)
                OPTIONAL MATCH (consumer:BazelTarget)-[:DEPENDS_ON*1..3]->
                               (genrule:BazelTarget {label: gc.bazel_label})
                OPTIONAL MATCH (consumer)-[:OWNED_BY]->(team:Team)
                RETURN s,
                       collect(DISTINCT e.path + ' ' + e.http_method) AS endpoints,
                       collect(DISTINCT {
                           label: gc.bazel_label,
                           language: gc.target_language
                       }) AS clients,
                       collect(DISTINCT {
                           label: consumer.label,
                           team: team.name,
                           kind: consumer.kind,
                           via_client: gc.bazel_label
                       }) AS consumers
                LIMIT 1
            """,
                path=swagger_path,
            )
            rec = await result.single()

        if rec is None:
            return SwaggerBlastRadiusResult(
                meta=meta,
                swagger_path=swagger_path,
                warnings=[Warning(code="NOT_FOUND", message="Swagger schema not found in graph")],
            )

        schema_node = rec["s"]
        clients = [
            AffectedClient(
                label=c.get("label"),
                language=c.get("language") or "UNKNOWN",
                needs_regeneration=True,
                last_generated_at_commit=None,
            )
            for c in (rec.get("clients") or [])
            if c and c.get("label")
        ]

        consumers = [
            AffectedConsumer(
                label=c.get("label"),
                team=c.get("team"),
                consumes_via=c.get("via_client") or "UNKNOWN",
                kind=c.get("kind") or "UNKNOWN",
                risk="HIGH",
            )
            for c in (rec.get("consumers") or [])
            if c and c.get("label")
        ]

        meta.nodes_traversed += len(clients) + len(consumers) + (1 if schema_node else 0)

        return SwaggerBlastRadiusResult(
            meta=meta,
            swagger_path=swagger_path,
            openapi_version=schema_node.get("openapi_version") if schema_node else None,
            endpoint_count=schema_node.get("endpoint_count", 0) if schema_node else 0,
            exposed_endpoints=rec.get("endpoints") or [],
            affected_generated_clients=clients,
            affected_consumer_services=consumers,
            recommended_actions=[
                "Regenerate generated clients: bazel build for each affected target",
                "Notify owning teams of affected consumers",
            ],
        )

    return await timed_operation(3, _run)
