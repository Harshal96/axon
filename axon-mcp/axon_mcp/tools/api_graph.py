from urllib.parse import quote

from axon_mcp.tools._base import proxy_call


async def handle(config, arguments, method, path) -> dict:
    endpoint_id = quote(str(arguments.get("endpoint_id", "")), safe="")
    params = {"depth": arguments.get("depth", 2)}
    return await proxy_call(
        "GET", f"/api/v2/apis/{endpoint_id}/graph", params=params)
