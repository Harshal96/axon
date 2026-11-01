import pytest
from axon_mcp.server import list_tools

@pytest.mark.asyncio
async def test_server_list_tools():
    tools = await list_tools()
    assert len(tools) == 8
    assert {t.name for t in tools} == {
        "axon_search", "axon_services", "axon_api_graph",
        "axon_function_apis", "axon_ask", "axon_api_explain", "axon_client_show", "axon_refs",
    }


@pytest.mark.asyncio
async def test_function_filters_are_optional_and_forwarded(monkeypatch):
    from axon_mcp.tools import function_apis

    calls = []

    async def proxy(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return []

    monkeypatch.setattr(function_apis, "proxy_call", proxy)
    await function_apis.handle(None, {"q": "helper"}, None, None)
    assert calls[-1][2]["params"] == {"q": "helper"}
    filters = {"q": "helper", "repo": "sample", "used_in": ["sample/alpha"],
               "defined_in": ["sample/shared/libraries"]}
    await function_apis.handle(None, filters, None, None)
    assert calls[-1] == ("GET", "/api/v2/functions/apis", {"params": filters})
    definition = next(t for t in await list_tools() if t.name == "axon_function_apis")
    assert definition.inputSchema["required"] == ["q"]
    assert definition.inputSchema["properties"]["used_in"]["type"] == "array"
    assert definition.inputSchema["properties"]["defined_in"]["type"] == "array"


@pytest.mark.asyncio
async def test_diagnosis_tools_and_prompts(monkeypatch):
    from axon_mcp.server import get_prompt, list_prompts
    from axon_mcp.tools import api_explain, client_show, refs
    calls = []
    async def proxy(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"ok": True}
    for module in (api_explain, client_show, refs):
        monkeypatch.setattr(module, "proxy_call", proxy)
    await api_explain.handle(None, {"repo": "r", "api": "GET /x", "service": "a"}, None, None)
    await client_show.handle(None, {"repo": "r", "client": "sdk"}, None, None)
    await refs.handle(None, {"repo": "r", "kind": "service", "entity": "a"}, None, None)
    assert all(call[0] == "GET" for call in calls)
    assert calls[0][1] == "/api/v2/repos/r/api-explain"
    assert calls[1][1] == "/api/v2/repos/r/clients/sdk"
    assert calls[2][1] == "/api/v2/repos/r/refs/service/a"
    assert {p.name for p in await list_prompts()} == {"axon-maintain", "axon-onboard"}
    assert "dry-run" in (await get_prompt("axon-maintain")).messages[0].content.text
