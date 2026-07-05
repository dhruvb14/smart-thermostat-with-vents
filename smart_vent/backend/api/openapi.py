"""
OpenAPI / Swagger setup, wired directly on apispec + marshmallow (Issue #188).

This replaces the abandoned ``aiohttp-apispec`` package, which had not seen a
release since 2022 and pinned ``apispec<4`` / ``marshmallow<4`` — blocking the
marshmallow v4 upgrade. ``aiohttp-apispec`` was only a thin glue layer between
``apispec`` (OpenAPI spec generation) and ``webargs`` (request parsing). This
app never installed ``aiohttp-apispec``'s validation middleware, so its
``@request_schema`` / ``@response_schema`` / ``@docs`` decorators were used
purely for **documentation** — every handler parses and validates its own
request body. We therefore only need to reproduce the documentation half:

* lightweight decorators that record OpenAPI metadata on each handler, and
* ``apispec`` + ``MarshmallowPlugin`` to turn that metadata into a spec, served
  alongside a self-hosted Swagger UI (from ``swagger-ui-bundle``, so the docs
  work offline behind Home Assistant ingress — no CDN, CSP-safe).

The marshmallow schemas in ``schemas.py`` are unchanged; only the wiring moved.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from typing import Any

from aiohttp import web
from apispec import APISpec
from apispec.ext.marshmallow import MarshmallowPlugin
from swagger_ui_bundle import swagger_ui_path

# Methods we surface as OpenAPI operations. aiohttp auto-registers a HEAD route
# for every GET (allow_head=True) — skip HEAD/OPTIONS so they don't appear as
# duplicate operations in the spec.
_DOCUMENTED_METHODS = frozenset({"get", "post", "put", "patch", "delete"})

# Matches an aiohttp dynamic path segment, e.g. the "room_id" in
# "/api/rooms/{room_id}", so it can be declared as an OpenAPI path parameter.
_PATH_PARAM_RE = re.compile(r"\{([^}/:]+)(?::[^}]+)?\}")


def _meta(handler: Any) -> dict[str, Any]:
    """Return (creating on first use) the OpenAPI metadata dict on a handler.

    The shape is also inspected by ``tests/test_api_spec_enforcement.py``, which
    asserts ``responses`` carries a 200/201 entry, so responses stay keyed by
    string status code.
    """
    meta = getattr(handler, "__apispec__", None)
    if meta is None:
        meta = {"tags": [], "summary": "", "responses": {}}
        handler.__apispec__ = meta
    return meta


def docs(*, tags: list[str] | None = None, summary: str = "", **extra: Any) -> Any:
    """Attach operation metadata (tags, summary, …) to a route handler."""

    def decorator(handler: Any) -> Any:
        meta = _meta(handler)
        if tags is not None:
            meta["tags"] = list(tags)
        if summary:
            meta["summary"] = summary
        meta.update(extra)
        return handler

    return decorator


def request_schema(schema: Any) -> Any:
    """Document the JSON request-body schema for a handler.

    Documentation only: request bodies are parsed and validated inside the
    handlers themselves (this app never used a validation middleware), so this
    decorator does not change runtime behaviour.
    """

    def decorator(handler: Any) -> Any:
        _meta(handler)["request_schema"] = schema
        return handler

    return decorator


def response_schema(schema: Any, code: int = 200, description: str = "") -> Any:
    """Document a response-body schema for a given HTTP status code."""

    def decorator(handler: Any) -> Any:
        _meta(handler)["responses"][str(code)] = {
            "schema": schema,
            "description": description or "Successful response",
        }
        return handler

    return decorator


def query_params(params: list[dict[str, Any]]) -> Any:
    """Document query-string parameters for a handler (Issue #403).

    Documentation only — handlers still read ``request.rel_url.query`` and
    validate the values themselves. But declaring the params here flows them
    into the OpenAPI spec and, in turn, into the generated MCP tool input
    schemas (see ``mcp_openapi``), so an MCP caller can discover and pass
    date-range / paging knobs that were previously invisible.

    Each entry is a dict with keys ``name`` (required), ``schema`` (JSON Schema
    for the value, defaulting to a string), ``description`` (optional), and
    ``required`` (optional, default ``False``). Repeated decorations accumulate.
    """

    def decorator(handler: Any) -> Any:
        meta = _meta(handler)
        meta["query_params"] = meta.get("query_params", []) + [dict(p) for p in params]
        return handler

    return decorator


def _build_operation(meta: dict[str, Any]) -> dict[str, Any]:
    """Translate handler ``__apispec__`` metadata into an OpenAPI operation."""
    op: dict[str, Any] = {}
    if meta.get("tags"):
        op["tags"] = meta["tags"]
    if meta.get("summary"):
        op["summary"] = meta["summary"]
    if meta.get("description"):
        op["description"] = meta["description"]
    request = meta.get("request_schema")
    if request is not None:
        op["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": request}},
        }
    responses: dict[str, Any] = {}
    for code, info in meta.get("responses", {}).items():
        responses[code] = {
            "description": info.get("description", ""),
            "content": {"application/json": {"schema": info["schema"]}},
        }
    # Every OpenAPI operation must declare at least one response.
    op["responses"] = responses or {"default": {"description": ""}}
    query = meta.get("query_params")
    if query:
        op["parameters"] = [
            {
                "in": "query",
                "name": p["name"],
                "required": bool(p.get("required", False)),
                "schema": p.get("schema") or {"type": "string"},
                **({"description": p["description"]} if p.get("description") else {}),
            }
            for p in query
        ]
    return op


def build_spec(
    app: web.Application,
    *,
    title: str,
    version: str,
    openapi_version: str = "3.0.3",
) -> dict[str, Any]:
    """Build the OpenAPI document from every documented ``/api/`` route."""
    spec = APISpec(
        title=title,
        version=version,
        openapi_version=openapi_version,
        plugins=[MarshmallowPlugin()],
    )

    # Group operations by path so several methods on one path merge into a
    # single path item (and a single set of path parameters).
    by_path: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for route in app.router.routes():
        meta = getattr(route.handler, "__apispec__", None)
        if not meta:
            continue
        method = route.method.lower()
        if method not in _DOCUMENTED_METHODS:
            continue
        resource = route.resource
        path = resource.canonical if resource is not None else None
        if not path or not path.startswith("/api/"):
            continue
        by_path.setdefault(path, {})[method] = _build_operation(meta)

    for path, operations in by_path.items():
        params = [
            {"in": "path", "name": name, "required": True, "schema": {"type": "string"}}
            for name in _PATH_PARAM_RE.findall(path)
        ]
        if params:
            spec.path(path=path, operations=operations, parameters=params)
        else:
            spec.path(path=path, operations=operations)

    result: dict[str, Any] = spec.to_dict()
    return result


# Swagger UI page. Asset URLs are RELATIVE (./static/…, ./openapi.json) so the
# page works behind Home Assistant ingress, where the add-on is mounted under an
# opaque prefix. Inline init script is permitted by the app CSP
# (script-src 'self' 'unsafe-inline').
_SWAGGER_HTML = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Plenum API — Swagger UI</title>
    <link rel="stylesheet" href="./static/swagger-ui.css" />
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="./static/swagger-ui-bundle.js"></script>
    <script>
      window.onload = function () {
        window.ui = SwaggerUIBundle({
          url: "./openapi.json",
          dom_id: "#swagger-ui",
          deepLinking: true,
        });
      };
    </script>
  </body>
</html>
"""


def setup_openapi(
    app: web.Application,
    *,
    title: str = "Plenum API",
    version: str = "v1",
    url: str = "/api/docs/openapi.json",
    swagger_path: str = "/api/docs/",
    static_path: str = "/api/docs/static",
) -> None:
    """Generate the OpenAPI spec and mount the JSON, Swagger UI, and assets.

    Must be called after the API routes are registered (``app.add_routes``) so
    the spec can be built from their ``__apispec__`` metadata.
    """
    spec_json = json.dumps(build_spec(app, title=title, version=version))

    async def openapi_json(_request: web.Request) -> web.Response:
        return web.Response(text=spec_json, content_type="application/json")

    async def swagger_ui(_request: web.Request) -> web.Response:
        return web.Response(text=_SWAGGER_HTML, content_type="text/html")

    app.router.add_get(url, openapi_json)
    app.router.add_get(swagger_path, swagger_ui)

    # Redirect the bare path (/api/docs) to the trailing-slash form so the
    # page's relative asset URLs resolve correctly.
    bare = swagger_path.rstrip("/")
    if bare and bare != swagger_path:

        async def docs_redirect(_request: web.Request) -> web.StreamResponse:
            raise web.HTTPFound(swagger_path)

        app.router.add_get(bare, docs_redirect)

    # Serve the bundled Swagger UI assets locally (offline / ingress / CSP-safe).
    app.router.add_static(static_path, swagger_ui_path)
