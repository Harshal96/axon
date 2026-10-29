"""
Analysis operations:
  - dead_code: symbols with no callers
  - hub_nodes: most-called symbols (architectural hotspots)
  - team_coupling: cross-team dependency matrix
"""

from __future__ import annotations

import logging
from typing import Any

from axon.db.connection import async_session
from axon.graph.envelope import timed_operation
from axon.graph.models import (
    CouplingEntry,
    DeadCodeResult,
    DeadSymbol,
    GraphMeta,
    HubNode,
    HubNodesResult,
    TeamCouplingResult,
    Warning,
)

logger = logging.getLogger(__name__)


async def dead_code(target_label: str, language: str | None = None) -> DeadCodeResult:
    """
    Find public, non-test symbols in a target that have no incoming CALLS edges.
    Excludes symbols in test files and private symbols (starting with _).
    """

    async def _run(meta: GraphMeta) -> DeadCodeResult:
        lang_filter = "AND s.language = $language" if language else ""
        params: dict[str, Any] = {"label": target_label}
        if language:
            params["language"] = language.upper()

        async with async_session() as session:
            result = await session.run(
                f"""
                MATCH (t:BazelTarget {{label: $label}})-[:BUILDS]->(f:SourceFile)
                MATCH (f)-[:DEFINES]->(s:Symbol)
                WHERE NOT (s)<-[:CALLS]-()
                  AND s.is_public = true
                  AND s.is_test = false
                  AND s.kind IN ['FUNCTION', 'METHOD']
                  {lang_filter}
                RETURN s.name AS name,
                       s.qualified_name AS qualified_name,
                       s.kind AS kind,
                       f.path AS file_path,
                       s.line_start AS line
                ORDER BY file_path, line
            """,
                **params,
            )
            records = await result.data()

        dead = [
            DeadSymbol(
                name=r["name"],
                qualified_name=r["qualified_name"],
                kind=r["kind"],
                file_path=r["file_path"],
                line=r["line"],
            )
            for r in records
        ]

        warnings: list[Warning] = []
        if len(dead) > 50:
            warnings.append(
                Warning(
                    code="LARGE_DEAD_CODE_SET",
                    message=f"{len(dead)} potentially dead symbols — verify before deleting.",
                )
            )

        meta.nodes_traversed += len(records)

        return DeadCodeResult(
            meta=meta,
            target=target_label,
            language=language,
            potentially_dead_symbols=dead,
            total=len(dead),
            warnings=warnings,
        )

    return await timed_operation(1, _run)


async def hub_nodes(package: str | None = None, top_n: int = 20) -> HubNodesResult:
    """
    Find the most-called symbols (highest in-degree) — architectural hotspots.
    """
    top_n = min(top_n, 100)

    async def _run(meta: GraphMeta) -> HubNodesResult:
        pkg_filter = (
            "WHERE ($package IS NULL OR t.package = $package)" if package is not None else ""
        )
        params: dict[str, Any] = {"top_n": top_n}
        if package is not None:
            params["package"] = package

        async with async_session() as session:
            result = await session.run(
                f"""
                MATCH (s:Symbol)<-[r:CALLS]-()
                OPTIONAL MATCH (f:SourceFile)-[:DEFINES]->(s)
                OPTIONAL MATCH (t:BazelTarget)-[:BUILDS]->(f)
                {pkg_filter}
                RETURN s.name AS name,
                       s.qualified_name AS qualified_name,
                       s.kind AS kind,
                       count(r) AS call_count,
                       f.path AS file_path,
                       t.label AS target_label
                ORDER BY call_count DESC
                LIMIT $top_n
            """,
                **params,
            )
            records = await result.data()

        hubs = [
            HubNode(
                name=r["name"],
                qualified_name=r["qualified_name"],
                kind=r["kind"],
                call_count=r["call_count"],
                file_path=r["file_path"],
                target_label=r["target_label"],
            )
            for r in records
        ]

        meta.nodes_traversed += len(records)

        return HubNodesResult(
            meta=meta,
            package_filter=package,
            top_n=top_n,
            hub_nodes=hubs,
        )

    return await timed_operation(1, _run)


async def team_coupling() -> TeamCouplingResult:
    """Cross-team dependency matrix — how tightly coupled are teams?"""

    async def _run(meta: GraphMeta) -> TeamCouplingResult:
        async with async_session() as session:
            result = await session.run("""
                MATCH (from_t:BazelTarget)-[:DEPENDS_ON]->(to_t:BazelTarget)
                MATCH (from_t)-[:OWNED_BY]->(from_team:Team)
                MATCH (to_t)-[:OWNED_BY]->(to_team:Team)
                WHERE from_team.name <> to_team.name
                RETURN from_team.name AS from_team,
                       to_team.name AS to_team,
                       count(*) AS dep_count
                ORDER BY dep_count DESC
            """)
            records = await result.data()

        matrix = [
            CouplingEntry(
                from_team=r["from_team"],
                to_team=r["to_team"],
                dep_count=r["dep_count"],
            )
            for r in records
        ]

        # Identify most-coupled pairs
        warnings: list[Warning] = []
        if matrix and matrix[0].dep_count > 20:
            top = matrix[0]
            warnings.append(
                Warning(
                    code="HIGH_TEAM_COUPLING",
                    message=(
                        f"{top.from_team} → {top.to_team}: "
                        f"{top.dep_count} cross-team dependencies. Consider decoupling."
                    ),
                )
            )

        meta.nodes_traversed += len(records)

        return TeamCouplingResult(
            meta=meta,
            coupling_matrix=matrix,
            total_cross_team_deps=sum(r.dep_count for r in matrix),
            warnings=warnings,
        )

    return await timed_operation(1, _run)
