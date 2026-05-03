import asyncio
from aiohttp import web
from smart_vent.backend.api.routes import routes
from aiohttp_apispec import setup_aiohttp_apispec
from aiohttp_apispec.aiohttp_apispec import AiohttpApiSpec

app = web.Application()
app.add_routes(routes)

original_get_index_page = AiohttpApiSpec._get_index_page
def patched_get_index_page(self, app_obj, static_files, static_path_str):
    html = original_get_index_page(self, app_obj, static_files, static_path_str)
    return html.replace('"/api/docs/', '"./')
AiohttpApiSpec._get_index_page = patched_get_index_page

setup_aiohttp_apispec(
    app=app,
    title="Plenum API",
    version="v1",
    url="/api/docs/openapi.json",
    swagger_path="/api/docs/",
    static_path="/api/docs/static",
)

app.router.add_get("/api/docs", lambda r: web.HTTPFound("/api/docs/"))

async def test():
    # Simulate a request to the swagger view
    client = web.Server(app._make_handler())
    # We can just call the view
    for route in app.router.routes():
        if getattr(route, 'name', None) == 'swagger.docs':
            class MockRequest:
                app = app
            resp = await route.handler(MockRequest())
            print(resp.text)
            break

asyncio.run(test())
