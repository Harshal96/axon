from axon_mcp.tools._base import proxy_call


async def handle(config, arguments, method, path) -> dict:
    params = {}
    if arguments.get("repo"):
        params["repo"] = arguments["repo"]
    return await proxy_call("GET", "/api/v2/services", params=params)
