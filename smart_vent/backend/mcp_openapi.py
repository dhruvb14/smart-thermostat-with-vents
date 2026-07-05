"""Generate MCP tool definitions from Plenum's OpenAPI spec.

This is the single source of truth that lets the HTTP MCP server expose 100% of
the REST surface without hand-writing a tool per endpoint. Each documented
``/api/`` operation becomes one MCP tool whose JSON input schema is derived from
the route's path parameters, its declared query parameters (Issue #403), and its
marshmallow request-body schema. The tool,
when called, is dispatched back through the *running* aiohttp server over
loopback (see ``mcp_http.py``) — so validation, unit conversion (``_to_f`` /
``_delta_to_f``), logging, and WebSocket broadcasts all happen exactly once in
the route handler. There is no second copy of the business logic, which is what
keeps the #231 double-conversion class of bug from reappearing on the MCP path.

Pure functions only — no network, no server — so the generator is unit-testable
in isolation.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from aiohttp import web

from .api.openapi import build_spec

# Path items carry a sibling ``parameters`` key alongside the HTTP verbs; only
# these are real operations.
_HTTP_METHODS = ("get", "post", "put", "patch", "delete")

# ``{room_id}`` or the (already-canonicalised) ``{entity_id}`` — the regex form
# ``{entity_id:.*}`` is stripped to ``{entity_id}`` by aiohttp's canonical path,
# but tolerate the ``:regex`` suffix just in case.
_PATH_PARAM_RE = re.compile(r"\{([^}/:]+)(?::[^}]+)?\}")


@dataclass
class ToolSpec:
    """One MCP tool generated from an OpenAPI operation."""

    name: str
    method: str  # lower-case HTTP verb
    path_template: str  # e.g. "/api/rooms/{room_id}/schedules"
    description: str
    input_schema: dict[str, Any]
    path_params: list[str] = field(default_factory=list)
    body_props: set[str] = field(default_factory=set)
    query_props: set[str] = field(default_factory=set)

    def build_request(self, arguments: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """Turn tool arguments into a concrete (relative URL, JSON body).

        Path parameters fill the template; declared query parameters (Issue
        #403) are appended as the URL query string; the remaining known body
        fields form the JSON payload. Query params the OpenAPI spec does not
        declare still fall back to their endpoint defaults.
        """
        url = self.path_template
        for name in self.path_params:
            value = arguments.get(name, "")
            url = _fill_path_param(url, name, str(value))
        query = {
            k: arguments[k] for k in self.query_props if k in arguments and arguments[k] is not None
        }
        if query:
            url = f"{url}?{urlencode(query, doseq=True)}"
        body = {k: v for k, v in arguments.items() if k in self.body_props}
        # GET/DELETE never carry a body; write verbs send one even if empty so
        # the handler's ``await request.json()`` does not blow up on no payload.
        if self.method in ("get", "delete"):
            return url, None
        return url, body


def _fill_path_param(url: str, name: str, value: str) -> str:
    """Replace ``{name}`` or ``{name:regex}`` in *url* with *value*."""
    return re.sub(r"\{" + re.escape(name) + r"(?::[^}]+)?\}", value.replace("\\", "\\\\"), url)


def _rewrite_refs(obj: Any) -> Any:
    """Rewrite ``#/components/schemas/X`` refs to ``#/$defs/X`` in-place-ish.

    The generated tool input schema embeds the component schemas under ``$defs``
    so it is self-contained (MCP clients receive one resolvable JSON Schema).
    """
    if isinstance(obj, dict):
        return {
            k: (
                v.replace("#/components/schemas/", "#/$defs/")
                if k == "$ref" and isinstance(v, str)
                else _rewrite_refs(v)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_rewrite_refs(v) for v in obj]
    return obj


def _tool_name(method: str, path: str) -> str:
    """Derive a stable, MCP-safe tool name from a verb + path."""
    slug = path.replace("/api/", "", 1).strip("/")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", slug).strip("_")
    return f"{method}_{slug}".lower() if slug else method


def build_tool_specs(app: web.Application) -> list[ToolSpec]:
    """Generate one :class:`ToolSpec` per documented ``/api/`` operation."""
    spec = build_spec(app, title="Plenum API", version="v1")
    components = spec.get("components", {}).get("schemas", {})
    defs = _rewrite_refs(copy.deepcopy(components)) if components else {}

    specs: list[ToolSpec] = []
    seen: set[str] = set()

    for path, item in spec.get("paths", {}).items():
        for method, op in item.items():
            if method not in _HTTP_METHODS:
                continue  # skips the sibling "parameters" list

            name = _tool_name(method, path)
            # Uniqueness guard — collisions are unlikely but cheap to defend.
            if name in seen:
                suffix = 2
                while f"{name}_{suffix}" in seen:
                    suffix += 1
                name = f"{name}_{suffix}"
            seen.add(name)

            path_params = _PATH_PARAM_RE.findall(path)
            properties: dict[str, Any] = {}
            required: list[str] = []
            for p in path_params:
                properties[p] = {"type": "string", "description": f"Path parameter '{p}'"}
                required.append(p)

            # Query parameters (Issue #403): surface declared ?start/?end/?days/
            # ?limit/?offset knobs as tool inputs so date-range and paged history
            # are reachable over MCP, not just via the REST UI.
            query_props: set[str] = set()
            for param in op.get("parameters", []):
                if param.get("in") != "query":
                    continue
                pname = param.get("name")
                if not pname or pname in properties:
                    continue
                pschema = dict(param.get("schema") or {"type": "string"})
                if param.get("description") and "description" not in pschema:
                    pschema["description"] = param["description"]
                properties[pname] = pschema
                query_props.add(pname)
                if param.get("required"):
                    required.append(pname)

            body_props: set[str] = set()
            request_body = op.get("requestBody")
            if request_body:
                schema = request_body["content"]["application/json"]["schema"]
                resolved = _resolve_ref(schema, components)
                resolved = _rewrite_refs(resolved)
                for key, subschema in resolved.get("properties", {}).items():
                    properties[key] = subschema
                    body_props.add(key)
                # NB: we deliberately do *not* copy the schema's ``required`` for
                # body fields. Plenum's marshmallow schemas are documentation-only
                # (handlers parse/validate their own bodies), so their ``required``
                # lists do not always match handler reality — e.g. the ``Room``
                # schema marks ``id`` required, but ``POST /api/rooms`` generates
                # the id server-side. Enforcing it here would reject valid calls.
                # Only path parameters (part of the URL) are truly required; the
                # handler returns a safe error for any body field it genuinely
                # needs.

            input_schema: dict[str, Any] = {"type": "object", "properties": properties}
            if required:
                input_schema["required"] = required
            if defs:
                input_schema["$defs"] = defs

            description = op.get("summary") or op.get("description") or f"{method.upper()} {path}"

            specs.append(
                ToolSpec(
                    name=name,
                    method=method,
                    path_template=path,
                    description=description,
                    input_schema=input_schema,
                    path_params=path_params,
                    body_props=body_props,
                    query_props=query_props,
                )
            )

    return specs


def _resolve_ref(schema: dict[str, Any], components: dict[str, Any]) -> dict[str, Any]:
    """Dereference a top-level ``$ref`` into the referenced component schema."""
    ref = schema.get("$ref")
    if not ref:
        return schema
    name = ref.rsplit("/", 1)[-1]
    return copy.deepcopy(components.get(name, {}))
