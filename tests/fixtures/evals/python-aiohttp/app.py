from aiohttp import web
routes = web.RouteTableDef()
@routes.get('/status')
async def status(request): pass
