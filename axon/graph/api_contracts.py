"""
api_contracts: full view of a service's API surface.
Returns endpoints it exposes, endpoints it consumes,
generated clients, and who consumes its API.
"""

from __future__ import annotations

import logging
import time

from axon.db.connection import async_session
from axon.graph.models import (
    ApiConsumerEntry,
    ApiContractsResult,
    EndpointSummary,
    GeneratedCodeRef,
)
from axon.graph.staleness import get_graph_meta

logger = logging.getLogger(__name__)


async def api_contracts(service_label: str) -> ApiContractsResult:
    """
    Get the full API contract view for a service:
    - endpoints it exposes (via swagger)
    - endpoints it consumes (via generated clients from other services)
    - generated clients it owns
    - who consumes its API
    """
    start = time.monotonic()
    meta = await get_graph_meta()

    async with async_session() as session:
        # Endpoints this service exposes
        exposes_result = await session.run(
            """
            MATCH (t:BazelTarget {label: $label})-[:EXPOSES_API]->(ep:ApiEndpoint)
            RETURN ep.endpoint_id AS endpoint_id,
                   ep.http_method AS method,
                   ep.path AS path,
                   ep.operation_id AS operation_id,
                   ep.is_deprecated AS is_deprecated,
                   ep.schema_hash AS schema_hash
        """,
            label=service_label,
        )
        exposes_records = await exposes_result.data()

        # Generated clients this service produces
        gen_result = await session.run(
            """
            MATCH (t:BazelTarget)
            WHERE t.has_swagger_src = true
              AND t.package = $package
            RETURN t.label AS label,
                   t.genrule_srcs AS srcs
        """,
            package=service_label.rsplit(":", 1)[0] if ":" in service_label else service_label,
        )
        gen_records = await gen_result.data()

        # Endpoints this service consumes via generated clients
        consumes_result = await session.run(
            """
            MATCH (t:BazelTarget {label: $label})-[:DEPENDS_ON*1..3]->(dep:BazelTarget)
            WHERE dep.has_swagger_src = true
            MATCH (gc:GeneratedClient {bazel_label: dep.label})-[:GENERATED_FROM]->(sw:SwaggerSchema)
            MATCH (sw)-[:DEFINES_ENDPOINT]->(ep:ApiEndpoint)
            RETURN DISTINCT ep.endpoint_id AS endpoint_id,
                            ep.http_method AS method,
                            ep.path AS path,
                            ep.operation_id AS operation_id,
                            ep.is_deprecated AS is_deprecated,
                            ep.schema_hash AS schema_hash
        """,
            label=service_label,
        )
        consumes_records = await consumes_result.data()

        # Who consumes this service's API
        consumers_result = await session.run(
            """
            MATCH (t:BazelTarget {label: $label})-[:EXPOSES_API]->(ep:ApiEndpoint)
            MATCH (consumer:BazelTarget)-[:CONSUMES_API]->(ep)
            WHERE consumer.label <> $label
            OPTIONAL MATCH (consumer)-[:OWNED_BY]->(team:Team)
            OPTIONAL MATCH (gc:GeneratedClient)-[:GENERATED_FROM]->(:SwaggerSchema)-[:DEFINES_ENDPOINT]->(ep)
            RETURN DISTINCT consumer.label AS label,
                            consumer.kind AS kind,
                            team.name AS team,
                            gc.bazel_label AS via_client,
                            gc.target_language AS language
        """,
            label=service_label,
        )
        consumers_records = await consumers_result.data()

    exposes = [
        EndpointSummary(
            endpoint_id=r.get("endpoint_id"),
            method=r.get("method", ""),
            path=r.get("path", ""),
            operation_id=r.get("operation_id"),
            is_deprecated=r.get("is_deprecated", False),
            schema_hash=r.get("schema_hash", ""),
        )
        for r in exposes_records
    ]

    consumes = [
        EndpointSummary(
            endpoint_id=r.get("endpoint_id"),
            method=r.get("method", ""),
            path=r.get("path", ""),
            operation_id=r.get("operation_id"),
            is_deprecated=r.get("is_deprecated", False),
            schema_hash=r.get("schema_hash", ""),
        )
        for r in consumes_records
    ]

    generated_clients = [
        GeneratedCodeRef(
            label=r["label"],
            generated_from=r["srcs"][0] if r.get("srcs") else "unknown",
            warning=f"DO NOT EDIT — regenerate with: bazel build {r['label']}",
        )
        for r in gen_records
    ]

    consumers_of_my_api = [
        ApiConsumerEntry(
            label=r["label"],
            kind=r.get("kind"),
            team=r.get("team"),
            via_client=r.get("via_client"),
            language=r.get("language"),
        )
        for r in consumers_records
    ]

    total_records = (
        len(exposes_records) + len(gen_records) + len(consumes_records) + len(consumers_records)
    )
    duration_ms = int((time.monotonic() - start) * 1000)
    meta.query_duration_ms = duration_ms
    meta.nodes_traversed = total_records

    return ApiContractsResult(
        meta=meta,
        service_label=service_label,
        exposes=exposes,
        consumes=consumes,
        generated_clients=generated_clients,
        consumers_of_my_api=consumers_of_my_api,
    )
