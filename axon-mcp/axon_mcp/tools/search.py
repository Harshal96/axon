from axon_mcp.tools._base import proxy_call


async def handle(config, arguments, method, path) -> dict:
    params = {"q": arguments.get("q", ""), "limit": arguments.get("limit", 20)}
    return await proxy_call("GET", "/api/v2/search", params=params)
