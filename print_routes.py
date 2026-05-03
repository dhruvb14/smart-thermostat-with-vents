from aiohttp import web
from smart_vent.backend.api.routes import routes
from aiohttp_apispec import setup_aiohttp_apispec

app = web.Application()
app.add_routes(routes)
setup_aiohttp_apispec(
    app=app,
    title="Plenum API",
    version="v1",
    url="/api/docs/openapi.json",
    swagger_path="/api/docs",
    static_path="/api/docs/static",
)
for route in app.router.routes():
    if 'swagger' in route.name:
        print(route)
