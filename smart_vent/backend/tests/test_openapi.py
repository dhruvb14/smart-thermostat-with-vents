"""
Unit tests for the apispec-based OpenAPI wiring (Issue #188).

Covers the decorators that replaced aiohttp-apispec's @docs / @request_schema /
@response_schema, and the spec builder that replaced setup_aiohttp_apispec.
"""

from __future__ import annotations

from aiohttp import web
from marshmallow import Schema, fields

from backend.api import openapi


class _ReqSchema(Schema):
    name = fields.Str(required=True)


class _RespSchema(Schema):
    ok = fields.Bool()


def test_docs_decorator_records_metadata() -> None:
    @openapi.docs(tags=["x"], summary="Do a thing", description="Long form")
    async def handler(_request):
        return web.Response()

    meta = handler.__apispec__
    assert meta["tags"] == ["x"]
    assert meta["summary"] == "Do a thing"
    assert meta["description"] == "Long form"


def test_request_and_response_schema_decorators_share_one_metadata_dict() -> None:
    # Mirrors the real decorator stacking order in routes.py.
    @openapi.docs(tags=["rooms"], summary="Create")
    @openapi.request_schema(_ReqSchema)
    @openapi.response_schema(_RespSchema, code=201)
    async def handler(_request):
        return web.Response()

    meta = handler.__apispec__
    assert meta["request_schema"] is _ReqSchema
    # Enforcement test relies on string status keys carrying a schema.
    assert "201" in meta["responses"]
    assert meta["responses"]["201"]["schema"] is _RespSchema
    assert meta["responses"]["201"]["description"]  # default description filled in


def test_build_operation_default_response_when_none_documented() -> None:
    op = openapi._build_operation({"tags": [], "summary": "", "responses": {}})
    assert op["responses"] == {"default": {"description": ""}}


def test_build_spec_produces_paths_components_and_path_params() -> None:
    app = web.Application()

    @openapi.docs(tags=["rooms"], summary="Get a room")
    @openapi.response_schema(_RespSchema)
    async def get_room(_request):
        return web.Response()

    @openapi.docs(tags=["rooms"], summary="Create a room")
    @openapi.request_schema(_ReqSchema)
    @openapi.response_schema(_RespSchema, code=201)
    async def create_room(_request):
        return web.Response()

    # A non-/api route must be ignored even if it carries metadata.
    @openapi.docs(summary="not an api route")
    async def home(_request):
        return web.Response()

    app.router.add_get("/api/rooms/{room_id}", get_room)
    app.router.add_post("/api/rooms", create_room)
    app.router.add_get("/", home)

    spec = openapi.build_spec(app, title="Plenum API", version="v1")

    assert spec["openapi"] == "3.0.3"
    assert spec["info"] == {"title": "Plenum API", "version": "v1"}
    assert set(spec["paths"]) == {"/api/rooms/{room_id}", "/api/rooms"}

    # Path parameter declared for the dynamic segment.
    params = spec["paths"]["/api/rooms/{room_id}"]["parameters"]
    assert params == [
        {"in": "path", "name": "room_id", "required": True, "schema": {"type": "string"}}
    ]

    # Request body wired to the marshmallow schema (resolved to a $ref).
    post_body = spec["paths"]["/api/rooms"]["post"]["requestBody"]
    assert "$ref" in post_body["content"]["application/json"]["schema"]

    # MarshmallowPlugin registered the schemas as components.
    assert spec["components"]["schemas"]


def test_query_params_decorator_emitted_as_operation_parameters() -> None:
    # Issue #403 — @query_params flows into the operation's OpenAPI parameters.
    app = web.Application()

    @openapi.docs(tags=["metrics"], summary="Ranged")
    @openapi.query_params(
        [
            {"name": "start", "schema": {"type": "string", "format": "date"}},
            {
                "name": "days",
                "schema": {"type": "integer"},
                "description": "N days",
                "required": True,
            },
        ]
    )
    @openapi.response_schema(_RespSchema)
    async def ranged(_request):
        return web.Response()

    app.router.add_get("/api/ranged", ranged)
    spec = openapi.build_spec(app, title="Plenum API", version="v1")

    params = spec["paths"]["/api/ranged"]["get"]["parameters"]
    by_name = {p["name"]: p for p in params}
    assert by_name["start"]["in"] == "query"
    assert by_name["start"]["required"] is False
    assert by_name["start"]["schema"] == {"type": "string", "format": "date"}
    assert by_name["days"]["required"] is True
    assert by_name["days"]["description"] == "N days"


async def test_setup_openapi_serves_json_ui_and_redirect() -> None:
    from aiohttp.test_utils import TestClient, TestServer

    app = web.Application()

    @openapi.docs(tags=["system"], summary="Health")
    @openapi.response_schema(_RespSchema)
    async def healthz(_request):
        return web.Response()

    app.router.add_get("/api/healthz", healthz)
    openapi.setup_openapi(app, title="Plenum API", version="v1")

    async with TestClient(TestServer(app)) as client:
        await client.start_server()

        resp = await client.get("/api/docs/openapi.json")
        assert resp.status == 200
        data = await resp.json()
        assert data["info"]["title"] == "Plenum API"

        resp = await client.get("/api/docs/")
        assert resp.status == 200
        html = await resp.text()
        assert "Swagger UI" in html
        assert 'href="./static/swagger-ui.css"' in html
        assert 'src="./static/swagger-ui-bundle.js"' in html

        resp = await client.get("/api/docs", allow_redirects=False)
        assert resp.status == 302
        assert resp.headers["Location"] == "/api/docs/"
