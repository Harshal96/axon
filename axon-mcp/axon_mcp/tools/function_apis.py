from axon_mcp.tools._base import proxy_call


async def handle(config, arguments, method, path) -> dict:
    return await proxy_call(
        "GET", "/api/v2/functions/apis", params={"q": arguments.get("q", "")})
