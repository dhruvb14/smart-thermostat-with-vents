"""HTTP (Streamable HTTP) MCP server for Plenum.

Runs in the same process as the aiohttp add-on but on its own port, served by a
native ASGI stack (Starlette + uvicorn) so we never have to bridge aiohttp —
which is not ASGI — to the MCP SDK's transport. Tools are generated from the
OpenAPI spec (``mcp_openapi.build_tool_specs``) and each call is dispatched back
through the running REST API over loopback, keeping the route handlers as the
single source of truth (no duplicated business logic, no #231 double-conversion).

The MCP endpoint is gated by the ``mcp_enabled`` flag (the settings-cog toggle):
when disabled, ``/mcp`` returns ``503`` and no MCP session is established.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Callable

import aiohttp
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from .mcp_openapi import ToolSpec, build_tool_specs

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"

# Response content types we return verbatim as text. Anything else (e.g. the
# binary DB backup) is summarised rather than decoded into a tool result.
_TEXTUAL = ("application/json", "text/", "application/xml")


async def dispatch_tool(
    session: aiohttp.ClientSession,
    base_url: str,
    spec: ToolSpec,
    arguments: dict,
) -> list[types.ContentBlock] | types.CallToolResult:
    """Dispatch one tool call to the REST API over loopback.

    Returns the response body as text content, or a ``CallToolResult`` flagged
    ``isError`` for non-2xx responses. The REST handlers already return
    user-safe error messages (never raw exception detail — see CLAUDE.md /
    CWE-209), so echoing the response body back to the caller is safe.
    """
    url_path, body = spec.build_request(arguments)
    url = base_url.rstrip("/") + url_path
    try:
        async with session.request(spec.method, url, json=body) as resp:
            raw = await resp.read()
            content_type = resp.headers.get("Content-Type", "")
            status = resp.status
    except aiohttp.ClientError:
        # Loopback failure means the REST server is unreachable/stopping. Do not
        # leak the underlying exception; surface a generic, safe message.
        log.exception("MCP loopback dispatch failed for %s %s", spec.method, url_path)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="Failed to reach the Plenum API")],
            isError=True,
        )

    is_textual = any(content_type.startswith(t) for t in _TEXTUAL)
    if is_textual or not raw:
        text = raw.decode("utf-8", "replace")
    else:
        text = f"<{len(raw)} bytes of {content_type or 'binary'}; not returned over MCP — use the REST endpoint {spec.path_template}>"

    if status >= 400:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"HTTP {status}: {text}")],
            isError=True,
        )
    return [types.TextContent(type="text", text=text)]


def build_mcp_server(
    specs: list[ToolSpec],
    session: aiohttp.ClientSession,
    base_url: str,
) -> Server:
    """Build the low-level MCP server that serves *specs* via loopback dispatch."""
    server: Server = Server("plenum")
    tools = [
        types.Tool(name=s.name, description=s.description, inputSchema=s.input_schema)
        for s in specs
    ]
    by_name = {s.name: s for s in specs}

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return tools

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict
    ) -> list[types.ContentBlock] | types.CallToolResult:
        spec = by_name.get(name)
        if spec is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Unknown tool: {name}")],
                isError=True,
            )
        return await dispatch_tool(session, base_url, spec, arguments)

    return server


def build_asgi_app(server: Server, is_enabled: Callable[[], bool]) -> Starlette:
    """Wrap the MCP *server* in a Starlette ASGI app, gated by *is_enabled*."""
    session_manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        if not is_enabled():
            response = JSONResponse(
                {"error": "MCP server is disabled. Enable it in Plenum's settings."},
                status_code=503,
            )
            await response(scope, receive, send)
            return
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    # A request to the bare /mcp is 307-redirected to /mcp/ (standard for the
    # MCP Streamable HTTP transport; SDK clients follow it). The redirect
    # preserves method and body, so POST works either way.
    return Starlette(routes=[Mount(MCP_PATH, app=handle_mcp)], lifespan=lifespan)


def build_mcp_asgi_app(
    aiohttp_app,
    session: aiohttp.ClientSession,
    base_url: str,
    is_enabled: Callable[[], bool],
) -> Starlette:
    """Convenience: generate specs from *aiohttp_app* and build the ASGI app."""
    specs = build_tool_specs(aiohttp_app)
    log.info("MCP server exposing %d tools (generated from the OpenAPI spec)", len(specs))
    server = build_mcp_server(specs, session, base_url)
    return build_asgi_app(server, is_enabled)
