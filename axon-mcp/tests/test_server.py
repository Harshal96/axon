import pytest
from axon_mcp.server import list_tools

@pytest.mark.asyncio
async def test_server_list_tools():
    tools = await list_tools()
    assert len(tools) == 5
    assert {t.name for t in tools} == {
        "axon_search", "axon_services", "axon_api_graph",
        "axon_function_apis", "axon_ask",
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
