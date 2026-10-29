"""
GET /api/v1/graph/targets          — list targets, filterable by package/kind/team
GET /api/v1/graph/targets/{label}  — single target with its direct deps
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from axon.db.connection import async_session

router = APIRouter()


class TargetSummary(BaseModel):
    label: str
    kind: str
    package: str
    team_owner: str | None = None
    is_genrule: bool = False
    has_swagger_src: bool = False
    testonly: bool = False


class TargetDetail(TargetSummary):
    visibility: list[str] = []
    tags: list[str] = []
    direct_deps: list[str] = []
    genrule_srcs: list[str] = []
    genrule_outs: list[str] = []


class TargetListResponse(BaseModel):
    total: int
    targets: list[TargetSummary]


@router.get("/graph/targets", response_model=TargetListResponse)
async def list_targets(
    package: str | None = Query(None, description="Filter by Bazel package"),
    kind: str | None = Query(None, description="Filter by kind, e.g. PY_LIBRARY"),
    team: str | None = Query(None, description="Filter by team_owner"),
    limit: int = Query(100, le=1000),
    offset: int = Query(0, ge=0),
) -> TargetListResponse:
    filters = []
    params: dict = {"limit": limit, "offset": offset}

    if package:
        filters.append("t.package = $package")
        params["package"] = package
    if kind:
        filters.append("t.kind = $kind")
        params["kind"] = kind
    if team:
        filters.append("t.team_owner = $team")
        params["team"] = team

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    count_cypher = f"MATCH (t:BazelTarget) {where} RETURN count(t) AS total"
    cypher = f"""
        MATCH (t:BazelTarget) {where}
        RETURN t ORDER BY t.label SKIP $offset LIMIT $limit
    """

    async with async_session() as session:
        count_result = await session.run(count_cypher, **params)
        count_record = await count_result.single()
        total = count_record["total"] if count_record else 0

        result = await session.run(cypher, **params)
        records = await result.data()

    targets = []
    for rec in records:
        node = rec["t"]
        targets.append(
            TargetSummary(
                label=node.get("label", ""),
                kind=node.get("kind", ""),
                package=node.get("package", ""),
                team_owner=node.get("team_owner"),
                is_genrule=node.get("is_genrule", False),
                has_swagger_src=node.get("has_swagger_src", False),
                testonly=node.get("testonly", False),
            )
        )

    return TargetListResponse(total=total, targets=targets)


@router.get("/graph/targets/{label:path}", response_model=TargetDetail)
async def get_target(label: str) -> TargetDetail:
    cypher = """
        MATCH (t:BazelTarget {label: $label})
        OPTIONAL MATCH (t)-[:DEPENDS_ON]->(dep:BazelTarget)
        RETURN t, collect(dep.label) AS direct_deps
    """
    async with async_session() as session:
        result = await session.run(cypher, label=label)
        record = await result.single()

    if record is None:
        raise HTTPException(status_code=404, detail=f"Target not found: {label}")

    node = record["t"]
    return TargetDetail(
        label=node.get("label", ""),
        kind=node.get("kind", ""),
        package=node.get("package", ""),
        team_owner=node.get("team_owner"),
        is_genrule=node.get("is_genrule", False),
        has_swagger_src=node.get("has_swagger_src", False),
        testonly=node.get("testonly", False),
        visibility=node.get("visibility", []),
        tags=node.get("tags", []),
        direct_deps=record["direct_deps"],
        genrule_srcs=node.get("genrule_srcs", []),
        genrule_outs=node.get("genrule_outs", []),
    )


class MavenArtifactSummary(BaseModel):
    id: str
    group_id: str
    artifact_id: str
    version: str
    scope: str
    is_vulnerable: bool
    cve_ids: list[str]
    affected_targets: list[str]


@router.get("/graph/maven", response_model=list[MavenArtifactSummary])
async def list_maven_artifacts(
    vulnerable_only: bool = Query(False, alias="vulnerable"),
    group: str | None = Query(None),
    artifact: str | None = Query(None),
    version: str | None = Query(None),
    limit: int = Query(100, le=1000),
) -> list[MavenArtifactSummary]:
    """
    Query Maven artifacts in the graph.
    ?vulnerable=true returns only artifacts with known CVEs.
    """
    filters = []
    params: dict = {"limit": limit}

    if vulnerable_only:
        filters.append("m.is_vulnerable = true")
    if group:
        filters.append("m.group_id = $group")
        params["group"] = group
    if artifact:
        filters.append("m.artifact_id = $artifact")
        params["artifact"] = artifact
    if version:
        filters.append("m.version = $version")
        params["version"] = version

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    cypher = f"""
        MATCH (m:MavenArtifact)
        {where}
        OPTIONAL MATCH (t:BazelTarget)-[:MAVEN_DEP]->(m)
        RETURN m, collect(DISTINCT t.label) AS affected_targets
        ORDER BY m.group_id, m.artifact_id
        LIMIT $limit
    """
    async with async_session() as session:
        result = await session.run(cypher, **params)
        records = await result.data()

    return [
        MavenArtifactSummary(
            id=rec["m"]["id"],
            group_id=rec["m"]["group_id"],
            artifact_id=rec["m"]["artifact_id"],
            version=rec["m"]["version"],
            scope=rec["m"].get("scope", "COMPILE"),
            is_vulnerable=rec["m"].get("is_vulnerable", False),
            cve_ids=rec["m"].get("cve_ids", []),
            affected_targets=rec["affected_targets"],
        )
        for rec in records
    ]


class PnpmPackageSummary(BaseModel):
    id: str
    name: str
    version: str
    is_internal: bool
    workspace_path: str | None
    bazel_label: str | None
    consumer_count: int


@router.get("/graph/pnpm", response_model=list[PnpmPackageSummary])
async def list_pnpm_packages(
    internal_only: bool = Query(False, alias="internal"),
    name: str | None = Query(None),
    limit: int = Query(100, le=1000),
) -> list[PnpmPackageSummary]:
    """
    Query PnpmPackage nodes.
    ?internal=true returns only workspace packages.
    ?name=@company/ui-components returns exact matches.
    """
    filters = []
    params: dict = {"limit": limit}

    if internal_only:
        filters.append("p.is_internal = true")
    if name:
        filters.append("p.name = $name")
        params["name"] = name

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    cypher = f"""
        MATCH (p:PnpmPackage)
        {where}
        OPTIONAL MATCH (t)-[:PNPM_DEP]->(p)
        RETURN p, count(DISTINCT t) AS consumer_count
        ORDER BY p.is_internal DESC, p.name
        LIMIT $limit
    """
    async with async_session() as session:
        result = await session.run(cypher, **params)
        records = await result.data()

    return [
        PnpmPackageSummary(
            id=rec["p"]["id"],
            name=rec["p"]["name"],
            version=rec["p"]["version"],
            is_internal=rec["p"].get("is_internal", False),
            workspace_path=rec["p"].get("workspace_path"),
            bazel_label=rec["p"].get("bazel_label"),
            consumer_count=rec["consumer_count"],
        )
        for rec in records
    ]
