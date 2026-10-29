"""
Tests for api_contracts graph operation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from axon.graph.api_contracts import api_contracts
from axon.graph.models import GraphMeta


def _mock_session_router():
    mock_session = AsyncMock()

    async def mock_run(*args, **kwargs):
        query = args[0] if args else kwargs.get("query", "")
        mock_result = AsyncMock()
        records = []
        if (
            "MATCH (t:BazelTarget {label: $label})-[:EXPOSES_API]->(ep:ApiEndpoint)" in query
            and "MATCH (consumer:BazelTarget)-[:CONSUMES_API]->(ep)" not in query
        ):
            records = [
                {
                    "endpoint_id": "1",
                    "method": "GET",
                    "path": "/foo",
                    "operation_id": "getFoo",
                    "is_deprecated": False,
                    "schema_hash": "abc",
                }
            ]
        elif "MATCH (t:BazelTarget)\n            WHERE t.has_swagger_src = true" in query:
            records = [{"label": "//gen:client", "srcs": ["//gen:swagger.json"]}]
        elif "MATCH (t:BazelTarget {label: $label})-[:DEPENDS_ON*1..3]->(dep:BazelTarget)" in query:
            records = [
                {
                    "endpoint_id": "2",
                    "method": "POST",
                    "path": "/bar",
                    "operation_id": "postBar",
                    "is_deprecated": False,
                    "schema_hash": "xyz",
                }
            ]
        elif "MATCH (consumer:BazelTarget)-[:CONSUMES_API]->(ep)" in query:
            records = [
                {
                    "label": "//consumer:app",
                    "kind": "PY_BINARY",
                    "team": "team-b",
                    "via_client": "//gen:client",
                    "language": "PYTHON",
                }
            ]

        mock_result.data = AsyncMock(return_value=records)
        mock_result.single = AsyncMock(return_value=records[0] if records else None)
        return mock_result

    mock_session.run = mock_run
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.asyncio
@patch("axon.graph.api_contracts.get_graph_meta", new=AsyncMock(return_value=GraphMeta()))
async def test_api_contracts_returns_all_sections() -> None:
    with patch("axon.graph.api_contracts.async_session", return_value=_mock_session_router()):
        resp = await api_contracts("//provider:lib")

    assert len(resp.exposes) == 1
    assert len(resp.generated_clients) == 1
    assert len(resp.consumes) == 1
    assert len(resp.consumers_of_my_api) == 1
