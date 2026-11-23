from urllib.parse import quote

from axon_mcp.tools._base import proxy_call


async def handle(config, arguments, method, path) -> dict:
    endpoint_id = str(arguments.get("endpoint_id", ""))
    params = {"depth": arguments.get("depth", 2)}
    result = await proxy_call(
        "GET", "/api/v1/api-graph", params={**params, "endpoint_id": endpoint_id}
    )
    if result.get("status_code") == 404 and result.get("details") == {"detail": "Not Found"}:
        encoded = quote(endpoint_id, safe="")
        return await proxy_call("GET", f"/api/v1/apis/{encoded}/graph", params=params)
    return result
