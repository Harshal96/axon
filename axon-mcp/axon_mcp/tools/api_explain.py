from urllib.parse import quote

from axon_mcp.tools._base import proxy_call


async def handle(config, arguments, method, path) -> dict:
    return await proxy_call("GET", "/api/v1/repos/" + quote(arguments["repo"], safe="") + "/api-explain", params={'api': arguments.get('api', ""), 'service': arguments.get('service', "")}, cache_ttl=0)
