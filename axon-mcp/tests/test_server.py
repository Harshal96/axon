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
