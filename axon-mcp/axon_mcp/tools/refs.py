from urllib.parse import quote

from axon_mcp.tools._base import proxy_call


async def handle(config, arguments, method, path) -> dict:
    return await proxy_call("GET", "/api/v2/repos/" + quote(arguments["repo"], safe="") + "/refs/" + quote(arguments["kind"], safe="") + "/" + quote(arguments["entity"], safe="") + "", params={}, cache_ttl=0)
