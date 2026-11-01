from axon_mcp.tools._base import proxy_call


async def handle(config, arguments, method, path) -> dict:
    params = {"q": arguments.get("q", "")}
    for key in ("repo", "used_in", "defined_in"):
        if key in arguments:
            params[key] = arguments[key]
    return await proxy_call("GET", "/api/v2/functions/apis", params=params)
