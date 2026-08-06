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
import contextvars
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable

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

# The granted scope of the bearer token that authenticated the *current* MCP
# request (Issue #373). Set by ``handle_mcp`` after validating the token and
# read by ``dispatch_tool`` so the loopback REST call can carry an
# ``X-Plenum-Scope`` header for the auth middleware to enforce. A ContextVar
# propagates cleanly through the async dispatch chain and stays per-request. None
# means "no scope constraint" — legacy open mode (require_auth off).
_mcp_scope: contextvars.ContextVar[str | None] = contextvars.ContextVar("mcp_scope", default=None)

# Response content types we return verbatim as text. Anything else (e.g. the
# binary DB backup) is summarised rather than decoded into a tool result.
_TEXTUAL = ("application/json", "text/", "application/xml")


def _stateless_from_env() -> bool:
    """Whether the Streamable HTTP transport runs stateless. Defaults to True.

    Plenum serves MCP statelessly in production: it is a single-instance add-on
    and stateless sessions avoid any session-affinity bookkeeping. The
    ``PLENUM_MCP_STATELESS`` escape hatch exists so the conformance suite
    (Issue #543) can also exercise the *stateful* path, where the server issues
    an ``Mcp-Session-Id`` and clients must echo it back — a genuinely different
    code path in the SDK's session manager, and one that real MCP clients rely
    on against other servers.

    Deliberately an environment variable rather than a ``config.yaml`` option:
    it is a test axis, not a user-facing feature, so it carries no add-on
    option, no ``run.sh`` read, and no UI control. Same precedent as
    ``PLENUM_CLOCK_OVERRIDE``, which is set only in ``docker-compose.test.yml``.
    """
    raw = os.environ.get("PLENUM_MCP_STATELESS")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no")


async def dispatch_tool(
    session: aiohttp.ClientSession,
    base_url: str,
    spec: ToolSpec,
    arguments: dict,
    internal_token: str,
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
        # Present the per-process internal token so the loopback request clears
        # the CSRF middleware. This is a random per-boot secret (not a guessable
        # static marker), so an off-process client cannot replay it.
        headers = {"X-Plenum-Internal": internal_token}
        # Carry the authenticated token's scope so the auth middleware enforces
        # read/write/destructive at the REST boundary (#373 dual-layer). Absent
        # in legacy open mode (require_auth off), where scope is not enforced.
        granted_scope = _mcp_scope.get()
        if granted_scope is not None:
            headers["X-Plenum-Scope"] = granted_scope
        async with session.request(spec.method, url, json=body, headers=headers) as resp:
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
    internal_token: str,
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
        return await dispatch_tool(session, base_url, spec, arguments, internal_token)

    return server


def _extract_bearer(scope: Scope) -> str | None:
    """Pull the bearer token out of an ASGI request's Authorization header."""
    for key, value in scope.get("headers", []):
        if key == b"authorization":
            text = value.decode("latin-1")
            if text[:7].lower() == "bearer ":
                return text[7:].strip() or None
            return None
    return None


# validate_bearer(token) -> granted scope, or None if the token is invalid.
ValidateBearer = Callable[[str], Awaitable[str | None]]


def build_asgi_app(
    server: Server,
    is_enabled: Callable[[], bool],
    *,
    require_auth: Callable[[], bool] | None = None,
    validate_bearer: ValidateBearer | None = None,
    stateless: bool | None = None,
) -> Starlette:
    """Wrap the MCP *server* in a Starlette ASGI app.

    Gated first by ``is_enabled`` (the ``mcp_enabled`` toggle → 503 when off).
    Then, when ``require_auth`` returns True, every request must carry a valid
    ``Authorization: Bearer <token>`` — validated by ``validate_bearer``, which
    returns the token's scope (stashed for the loopback dispatch) or None → 401.
    When ``require_auth`` is False (or not wired), access is open (legacy).

    ``stateless`` selects the transport mode; ``None`` (the default) resolves it
    from ``PLENUM_MCP_STATELESS`` — see :func:`_stateless_from_env`.
    """
    if stateless is None:
        stateless = _stateless_from_env()
    session_manager = StreamableHTTPSessionManager(
        app=server, json_response=True, stateless=stateless
    )

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        if not is_enabled():
            response = JSONResponse(
                {"error": "MCP server is disabled. Enable it in Plenum's settings."},
                status_code=503,
            )
            await response(scope, receive, send)
            return

        if require_auth is not None and require_auth():
            token = _extract_bearer(scope)
            granted = await validate_bearer(token) if (token and validate_bearer) else None
            if granted is None:
                response = JSONResponse(
                    {"error": "A valid MCP bearer token is required."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
            _mcp_scope.set(granted)
        else:
            # Legacy open mode — no scope constraint on the loopback dispatch.
            _mcp_scope.set(None)

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
    internal_token: str,
    *,
    require_auth: Callable[[], bool] | None = None,
    validate_bearer: ValidateBearer | None = None,
    stateless: bool | None = None,
) -> Starlette:
    """Convenience: generate specs from *aiohttp_app* and build the ASGI app."""
    specs = build_tool_specs(aiohttp_app)
    resolved_stateless = _stateless_from_env() if stateless is None else stateless
    log.info(
        "MCP server exposing %d tools (generated from the OpenAPI spec); transport=%s",
        len(specs),
        "stateless" if resolved_stateless else "stateful",
    )
    server = build_mcp_server(specs, session, base_url, internal_token)
    return build_asgi_app(
        server,
        is_enabled,
        require_auth=require_auth,
        validate_bearer=validate_bearer,
        stateless=resolved_stateless,
    )
