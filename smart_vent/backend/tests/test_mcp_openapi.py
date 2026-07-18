"""Unit tests for the OpenAPI → MCP tool generator (Issue #372)."""

from __future__ import annotations

import jsonschema
from aiohttp import web
from marshmallow import Schema, fields

from backend.api import openapi
from backend.main import build_app
from backend.mcp_openapi import (
    ToolSpec,
    _fill_path_param,
    _resolve_ref,
    _rewrite_refs,
    _tool_name,
    build_tool_specs,
)
from backend.tests.integration.fake_ha import FakeHomeAssistant


class _Req(Schema):
    name = fields.Str(required=True)
    system_wide_temp = fields.Float()


class _Resp(Schema):
    ok = fields.Bool()


def _mini_app() -> web.Application:
    app = web.Application()

    @openapi.docs(tags=["rooms"], summary="Create a room")
    @openapi.request_schema(_Req)
    @openapi.response_schema(_Resp, code=201)
    async def create(_r):
        return web.Response()

    @openapi.docs(tags=["rooms"], summary="Get a room")
    @openapi.response_schema(_Resp)
    async def get_one(_r):
        return web.Response()

    app.router.add_post("/api/rooms", create)
    app.router.add_get("/api/rooms/{room_id}", get_one)
    return app


def test_generates_a_tool_per_operation() -> None:
    specs = build_tool_specs(_mini_app())
    by_name = {s.name: s for s in specs}
    assert set(by_name) == {"post_rooms", "get_rooms_room_id"}

    create = by_name["post_rooms"]
    assert create.method == "post"
    # Body fields become top-level properties; path params are required.
    assert "name" in create.body_props
    assert "system_wide_temp" in create.body_props
    assert create.input_schema["properties"]["name"]["type"] == "string"

    get_one = by_name["get_rooms_room_id"]
    assert get_one.path_params == ["room_id"]
    assert get_one.input_schema["required"] == ["room_id"]


def test_body_required_is_not_enforced_only_path_params_are() -> None:
    # The marshmallow schema marks ``name`` required, but that is documentation
    # only; the generator must not force it (handlers validate their own bodies).
    create = next(s for s in build_tool_specs(_mini_app()) if s.name == "post_rooms")
    assert create.input_schema.get("required") is None or "name" not in create.input_schema.get(
        "required", []
    )


def test_build_request_splits_path_and_body() -> None:
    specs = build_tool_specs(_mini_app())
    create = next(s for s in specs if s.name == "post_rooms")
    url, body = create.build_request({"name": "Office", "system_wide_temp": 70, "bogus": 1})
    assert url == "/api/rooms"
    assert body == {"name": "Office", "system_wide_temp": 70}  # unknown key dropped

    get_one = next(s for s in specs if s.name == "get_rooms_room_id")
    url, body = get_one.build_request({"room_id": "r1"})
    assert url == "/api/rooms/r1"
    assert body is None  # GET never carries a body


def test_full_app_covers_whole_rest_surface_with_valid_schemas() -> None:
    app = build_app(FakeHomeAssistant(), ":memory:", frontend_dist=None, start_ha=False)  # type: ignore[arg-type]
    specs = build_tool_specs(app)

    # One tool per documented operation, names unique, schemas valid JSON Schema.
    spec = openapi.build_spec(app, title="Plenum API", version="v1")
    op_count = sum(
        1
        for methods in spec["paths"].values()
        for m in methods
        if m in ("get", "post", "put", "patch", "delete")
    )
    assert len(specs) == op_count
    assert len({s.name for s in specs}) == len(specs)
    for s in specs:
        jsonschema.Draft202012Validator.check_schema(s.input_schema)

    # Spot-check representative tools exist across the surface.
    names = {s.name for s in specs}
    assert {"get_healthz", "post_rooms", "get_rooms", "post_system_mcp"} <= names


def test_tool_name_edges() -> None:
    assert _tool_name("get", "/api/rooms/{room_id}") == "get_rooms_room_id"
    assert _tool_name("get", "/api/") == "get"  # empty slug falls back to the verb


def test_name_collision_gets_suffixed() -> None:
    app = web.Application()

    @openapi.docs(summary="a")
    @openapi.response_schema(_Resp)
    async def h1(_r):
        return web.Response()

    @openapi.docs(summary="b")
    @openapi.response_schema(_Resp)
    async def h2(_r):
        return web.Response()

    # "/api/a.b" and "/api/a_b" both slugify to "get_a_b".
    app.router.add_get("/api/a.b", h1)
    app.router.add_get("/api/a_b", h2)
    names = [s.name for s in build_tool_specs(app)]
    assert "get_a_b" in names
    assert "get_a_b_2" in names


def test_fill_path_param_handles_regex_suffix() -> None:
    assert _fill_path_param("/api/x/{id:.*}", "id", "sensor.a") == "/api/x/sensor.a"
    assert _fill_path_param("/api/x/{id}", "id", "42") == "/api/x/42"


def test_rewrite_refs_and_resolve_ref() -> None:
    obj = {"$ref": "#/components/schemas/Room", "nested": [{"$ref": "#/components/schemas/X"}]}
    rewritten = _rewrite_refs(obj)
    assert rewritten["$ref"] == "#/$defs/Room"
    assert rewritten["nested"][0]["$ref"] == "#/$defs/X"
    # Scalars pass through untouched.
    assert _rewrite_refs("plain") == "plain"

    components = {"Room": {"type": "object", "properties": {"name": {"type": "string"}}}}
    assert _resolve_ref({"$ref": "#/components/schemas/Room"}, components) == components["Room"]
    # No $ref → returned as-is.
    assert _resolve_ref({"type": "object"}, components) == {"type": "object"}


def test_toolspec_is_constructible() -> None:
    spec = ToolSpec(
        name="x", method="get", path_template="/api/x", description="d", input_schema={}
    )
    assert spec.path_params == []
    assert spec.body_props == set()
    assert spec.query_props == set()


def _query_app() -> web.Application:
    app = web.Application()

    @openapi.docs(tags=["metrics"], summary="Ranged")
    @openapi.query_params(
        [
            {"name": "start", "schema": {"type": "string", "format": "date"}},
            {"name": "days", "schema": {"type": "integer"}, "description": "N days"},
        ]
    )
    @openapi.response_schema(_Resp)
    async def ranged(_r):
        return web.Response()

    app.router.add_get("/api/ranged", ranged)
    return app


def test_query_params_become_tool_inputs() -> None:
    # Issue #403 — declared query params surface as MCP tool input properties.
    spec = next(s for s in build_tool_specs(_query_app()) if s.name == "get_ranged")
    assert spec.query_props == {"start", "days"}
    props = spec.input_schema["properties"]
    assert props["start"]["type"] == "string"
    assert props["days"]["type"] == "integer"
    assert props["days"]["description"] == "N days"
    # Optional query params are never forced required.
    assert spec.input_schema.get("required") is None


def test_build_request_appends_declared_query_string() -> None:
    spec = next(s for s in build_tool_specs(_query_app()) if s.name == "get_ranged")
    url, body = spec.build_request({"start": "2026-01-01", "days": 3, "bogus": "x"})
    assert body is None  # GET carries no body
    assert url.startswith("/api/ranged?")
    # Only declared query params are forwarded; unknown keys are dropped.
    assert "start=2026-01-01" in url
    assert "days=3" in url
    assert "bogus" not in url

    # Omitted query params produce a bare path (no trailing "?").
    url_bare, _ = spec.build_request({})
    assert url_bare == "/api/ranged"


def test_triple_name_collision_walks_suffixes() -> None:
    """Three paths slugifying to the same name get _2 and then _3."""
    app = web.Application()

    def _handler():
        @openapi.docs(summary="x")
        @openapi.response_schema(_Resp)
        async def h(_r):
            return web.Response()

        return h

    # "/api/a.b", "/api/a_b" and "/api/a-b" all slugify to "get_a_b".
    app.router.add_get("/api/a.b", _handler())
    app.router.add_get("/api/a_b", _handler())
    app.router.add_get("/api/a-b", _handler())
    names = sorted(s.name for s in build_tool_specs(app))
    assert names == ["get_a_b", "get_a_b_2", "get_a_b_3"]


def test_required_query_param_lands_in_required_list() -> None:
    app = web.Application()

    @openapi.docs(summary="Needs a start")
    @openapi.query_params([{"name": "start", "required": True}])
    @openapi.response_schema(_Resp)
    async def h(_r):
        return web.Response()

    app.router.add_get("/api/needy", h)
    spec = next(s for s in build_tool_specs(app) if s.name == "get_needy")
    assert "start" in spec.query_props
    assert spec.input_schema["required"] == ["start"]


def test_query_param_shadowing_path_param_is_ignored() -> None:
    """A declared query param that collides with a path param must not clobber
    the path-parameter schema entry."""
    app = web.Application()

    @openapi.docs(summary="Shadowed")
    @openapi.query_params([{"name": "room_id", "schema": {"type": "integer"}}])
    @openapi.response_schema(_Resp)
    async def h(_r):
        return web.Response()

    app.router.add_get("/api/shadow/{room_id}", h)
    spec = next(s for s in build_tool_specs(app) if s.name == "get_shadow_room_id")
    # Path param wins: stays a string, is not exposed as a query prop.
    assert spec.input_schema["properties"]["room_id"]["type"] == "string"
    assert spec.query_props == set()


def test_non_query_and_nameless_parameters_are_skipped(monkeypatch) -> None:
    """build_tool_specs must tolerate specs that declare header params or
    malformed (nameless) params — only well-formed query params become inputs."""
    from backend import mcp_openapi

    fake_spec = {
        "paths": {
            "/api/thing": {
                "get": {
                    "description": "d",
                    "parameters": [
                        {"in": "header", "name": "X-Custom", "schema": {"type": "string"}},
                        {"in": "query", "schema": {"type": "string"}},  # no name
                        {"in": "query", "name": "ok", "schema": {"type": "integer"}},
                    ],
                }
            }
        }
    }
    monkeypatch.setattr(mcp_openapi, "build_spec", lambda *_a, **_k: fake_spec)
    specs = mcp_openapi.build_tool_specs(web.Application())
    assert len(specs) == 1
    spec = specs[0]
    assert spec.query_props == {"ok"}
    assert set(spec.input_schema["properties"]) == {"ok"}
