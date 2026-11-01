from axon_mcp.tools._base import proxy_call


async def handle(config, arguments, method, path) -> dict:
    return await proxy_call(
        "POST", "/api/v1/ask",
        json_body={"question": arguments.get("question", "")})
