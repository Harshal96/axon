"""Swagger to generated client to consumer chain tests."""

from __future__ import annotations

import pytest

from axon.db.connection import sync_session
from axon.ops.blast_radius import blast_radius
from axon.ops.swagger_impact import swagger_impact

pytestmark = pytest.mark.tier2


def _one(cypher: str):
    with sync_session() as session:
        return session.run(cypher).single()


def test_swagger_schema_node_exists() -> None:
    record = _one(
        "MATCH (sw:SwaggerSchema {path: 'services/payment/swagger.json'}) RETURN sw"
    )
    assert record is not None


def test_generated_client_linked_to_swagger() -> None:
    record = _one(
        """
        MATCH (gc:GeneratedClient)-[:GENERATED_FROM]->(sw:SwaggerSchema)
        WHERE gc.bazel_label = '//services/payment:generate_payment_client_py'
        RETURN sw.path AS path
        """
    )
    assert record is not None
    assert record["path"] == "services/payment/swagger.json"


def test_api_endpoints_exist() -> None:
    record = _one(
        """
        MATCH (ep:ApiEndpoint)
        WHERE ep.swagger_source = 'services/payment/swagger.json'
        RETURN count(ep) AS count
        """
    )
    assert record["count"] >= 1


@pytest.mark.asyncio
async def test_blast_radius_of_generated_client_includes_checkout() -> None:
    result = await blast_radius("//services/payment:generate_payment_client_py")
    labels = [target.label for target in result.affected_targets]
    assert "//services/checkout:checkout_lib" in labels


@pytest.mark.asyncio
async def test_swagger_impact_lists_affected_clients() -> None:
    result = await swagger_impact("services/payment/swagger.json")
    labels = [client.label for client in result.affected_generated_clients]
    assert "//services/payment:generate_payment_client_py" in labels
