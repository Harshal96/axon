import pytest
from axon_mcp.tools import api_graph


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_graph_proxy_preserves_full_id_and_site_evidence(monkeypatch, legacy):
    endpoint_id = "endpoint::finance::ledger::GET:/v1/graph"
    evidence = {"edges": [{"sites": [{"evidence": {"line": 12}}]}]}
    requests = []

    async def fake_proxy(method, path, params):
        requests.append((method, path, params))
        if legacy and path == "/api/v1/api-graph":
            return {"status_code": 404, "details": {"detail": "Not Found"}}
        return evidence

    monkeypatch.setattr(api_graph, "proxy_call", fake_proxy)
    assert await api_graph.handle(None, {"endpoint_id": endpoint_id}, None, None) == evidence
    assert requests[0] == (
        "GET",
        "/api/v1/api-graph",
        {"endpoint_id": endpoint_id, "depth": 2},
    )
    assert len(requests) == (2 if legacy else 1)
    if legacy:
        assert requests[1][1].endswith("%2Fv1%2Fgraph/graph")
