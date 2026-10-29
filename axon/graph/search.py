"""
Search operations:
  - find_symbol: exact lookup by qualified_name
  - semantic_search: fulltext search over symbols
  - api_consumers: who calls a given API endpoint
  - service_map: high-level service dependency graph
"""

from __future__ import annotations

import logging

from axon.db.connection import async_session
from axon.graph.envelope import timed_operation
from axon.graph.models import (
    ApiConsumerEntry,
    ApiConsumersResult,
    FindSymbolResult,
    GraphMeta,
    SearchHit,
    SearchResult,
    ServiceEdge,
    ServiceMapResult,
    ServiceNode,
    SymbolDetail,
    Warning,
)

logger = logging.getLogger(__name__)


async def find_symbol(qualified_name: str) -> FindSymbolResult:
    """Exact lookup of a Symbol by qualified_name."""

    async def _run(meta: GraphMeta) -> FindSymbolResult:
        async with async_session() as session:
            result = await session.run(
                """
                MATCH (s:Symbol {qualified_name: $qname})
                OPTIONAL MATCH (f:SourceFile)-[:DEFINES]->(s)
                OPTIONAL MATCH (t:BazelTarget)-[:BUILDS]->(f)
                OPTIONAL MATCH (t)-[:OWNED_BY]->(team:Team)
                RETURN s, f, t, team
                LIMIT 1
            """,
                qname=qualified_name,
            )
            rec = await result.single()

        if rec is None:
            return FindSymbolResult(
                meta=meta,
                found=False,
                qualified_name=qualified_name,
                warnings=[
                    Warning(code="SYMBOL_NOT_FOUND", message=f"No symbol found: {qualified_name}")
                ],
            )

        sym = rec["s"]
        file_node = rec.get("f")
        target_node = rec.get("t")
        team_node = rec.get("team")

        meta.nodes_traversed += 1

        return FindSymbolResult(
            meta=meta,
            found=True,
            qualified_name=qualified_name,
            symbol=SymbolDetail(
                id=sym.get("id"),
                name=sym.get("name"),
                qualified_name=sym.get("qualified_name"),
                kind=sym.get("kind"),
                language=sym.get("language"),
                line_start=sym.get("line_start"),
                line_end=sym.get("line_end"),
                signature=sym.get("signature"),
                docstring=sym.get("docstring"),
                is_public=sym.get("is_public"),
                is_test=sym.get("is_test"),
            ),
            file={"path": file_node.get("path"), "language": file_node.get("language")}
            if file_node
            else None,
            target={"label": target_node.get("label"), "kind": target_node.get("kind")}
            if target_node
            else None,
            team=team_node.get("name") if team_node else None,
        )

    return await timed_operation(1, _run)


async def semantic_search(q: str, scope: str | None = None, limit: int = 20) -> SearchResult:
    """Fulltext search for symbols."""
    limit = min(limit, 100)

    async def _run(meta: GraphMeta) -> SearchResult:
        async with async_session() as session:
            result = await session.run(
                """
                CALL db.index.fulltext.queryNodes("idx_symbol_fulltext", $query) YIELD node, score
                WHERE $scope IS NULL OR node.id STARTS WITH $scope
                OPTIONAL MATCH (f:SourceFile)-[:DEFINES]->(node)
                OPTIONAL MATCH (t:BazelTarget)-[:BUILDS]->(f)
                OPTIONAL MATCH (t)-[:OWNED_BY]->(team:Team)
                RETURN node, score, f.path AS file_path, t.label AS bazel_label, team.name AS team_name
                LIMIT $limit
            """,
                query=q,
                scope=scope,
                limit=limit,
            )
            records = await result.data()

        hits = []
        for rec in records:
            node = rec["node"]
            hits.append(
                SearchHit(
                    node_type="Symbol",
                    node_id=node.get("id"),
                    name=node.get("name"),
                    qualified_name=node.get("qualified_name"),
                    kind=node.get("kind"),
                    file_path=rec.get("file_path"),
                    bazel_label=rec.get("bazel_label"),
                    team=rec.get("team_name"),
                    relevance_score=rec["score"],
                    snippet=node.get("docstring")[:100] if node.get("docstring") else None,
                )
            )

        meta.nodes_traversed += len(records)

        return SearchResult(meta=meta, query=q, total=len(hits), hits=hits)

    return await timed_operation(1, _run)


async def api_consumers(endpoint_path: str, method: str) -> ApiConsumersResult:
    """Find all BazelTargets that consume a given API endpoint."""

    async def _run(meta: GraphMeta) -> ApiConsumersResult:
        async with async_session() as session:
            result = await session.run(
                """
                MATCH (e:ApiEndpoint {path: $path, http_method: $method})
                MATCH (s:SwaggerSchema)-[:DEFINES_ENDPOINT]->(e)
                OPTIONAL MATCH (gc:GeneratedClient)-[:GENERATED_FROM]->(s)
                OPTIONAL MATCH (consumer:BazelTarget)-[:DEPENDS_ON*1..3]->
                               (genrule:BazelTarget {label: gc.bazel_label})
                OPTIONAL MATCH (consumer)-[:OWNED_BY]->(team:Team)
                RETURN e, s.path AS swagger_path,
                       gc.bazel_label AS client_label,
                       gc.target_language AS language,
                       consumer.label AS consumer_label,
                       consumer.kind AS consumer_kind,
                       team.name AS team_name
            """,
                path=endpoint_path,
                method=method.upper(),
            )
            records = await result.data()

        if not records:
            return ApiConsumersResult(
                meta=meta,
                endpoint_path=endpoint_path,
                method=method.upper(),
                consumers=[],
                total=0,
                warnings=[
                    Warning(
                        code="ENDPOINT_NOT_FOUND",
                        message=f"No endpoint: {method.upper()} {endpoint_path}",
                    )
                ],
            )

        consumers = [
            ApiConsumerEntry(
                label=r["consumer_label"],
                kind=r["consumer_kind"],
                team=r["team_name"],
                via_client=r["client_label"],
                language=r["language"],
            )
            for r in records
            if r.get("consumer_label")
        ]

        meta.nodes_traversed += len(records)

        return ApiConsumersResult(
            meta=meta,
            endpoint_path=endpoint_path,
            method=method.upper(),
            swagger_path=records[0].get("swagger_path") if records else None,
            consumers=consumers,
            total=len(consumers),
        )

    return await timed_operation(3, _run)


async def service_map() -> ServiceMapResult:
    """Return the high-level service dependency graph via API contracts."""

    async def _run(meta: GraphMeta) -> ServiceMapResult:
        async with async_session() as session:
            result = await session.run("""
                MATCH (s:SwaggerSchema)<-[:GENERATED_FROM]-(gc:GeneratedClient)
                MATCH (provider:BazelTarget {label: s.service_label})
                OPTIONAL MATCH (consumer:BazelTarget)-[:DEPENDS_ON*1..3]->
                               (genrule:BazelTarget {label: gc.bazel_label})
                WHERE consumer.kind IN ['PY_LIBRARY','PY_BINARY','JAVA_LIBRARY','JAVA_BINARY']
                OPTIONAL MATCH (provider)-[:OWNED_BY]->(pt:Team)
                OPTIONAL MATCH (consumer)-[:OWNED_BY]->(ct:Team)
                RETURN DISTINCT
                    provider.label AS provider,
                    provider.kind AS provider_kind,
                    consumer.label AS consumer,
                    pt.name AS provider_team,
                    ct.name AS consumer_team,
                    s.path AS swagger_path,
                    s.endpoint_count AS endpoint_count
                ORDER BY provider
            """)
            records = await result.data()

        services: dict[str, ServiceNode] = {}
        edges: list[ServiceEdge] = []

        for rec in records:
            for svc_label, svc_team, kind in [
                (rec.get("provider"), rec.get("provider_team"), rec.get("provider_kind")),
                (
                    rec.get("consumer"),
                    rec.get("consumer_team"),
                    None,
                ),  # consumer_kind not fetched here to keep it simple, wait, let's just infer language from label or keep UNKNOWN
            ]:
                if svc_label and svc_label not in services:
                    # simplistic language inference from Bazel kind
                    lang = "UNKNOWN"
                    if kind and "PY" in kind:
                        lang = "PYTHON"
                    if kind and "JAVA" in kind:
                        lang = "JAVA"

                    services[svc_label] = ServiceNode(
                        label=svc_label,
                        team=svc_team,
                        language=lang,
                        has_swagger=(svc_label == rec.get("provider")),
                    )

            if rec.get("provider") and rec.get("consumer"):
                edges.append(
                    ServiceEdge(
                        from_service=rec["consumer"],
                        to_service=rec["provider"],
                        swagger_path=rec.get("swagger_path"),
                        endpoint_count=rec.get("endpoint_count", 0),
                        via_generated_client=True,
                    )
                )

        meta.nodes_traversed += len(records)

        return ServiceMapResult(
            meta=meta,
            services=list(services.values()),
            api_dependencies=edges,
            total_services=len(services),
            total_api_links=len(edges),
        )

    return await timed_operation(3, _run)
