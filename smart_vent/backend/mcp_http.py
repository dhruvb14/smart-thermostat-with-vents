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

import contextvars
import logging
import os
from collections.abc import Awaitable, Callable

import aiohttp
import mcp.types as types
from mcp import MCPError
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

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


def _text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    """Build a tool result. v2 removed auto-wrapping, so results are explicit."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=is_error
    )


async def dispatch_tool(
    session: aiohttp.ClientSession,
    base_url: str,
    spec: ToolSpec,
    arguments: dict,
    internal_token: str,
) -> types.CallToolResult:
    """Dispatch one tool call to the REST API over loopback.

    Returns the response body as text content, or a ``CallToolResult`` flagged
    ``is_error`` for non-2xx responses. The REST handlers already return
    user-safe error messages (never raw exception detail — see CLAUDE.md /
    CWE-209), so echoing the response body back to the caller is safe.

    A *handled* HTTP failure stays an error-flagged result rather than becoming
    a JSON-RPC error: an upstream 4xx is information the caller asked for, not a
    protocol fault. Only genuinely unexpected exceptions escalate — see
    :func:`build_mcp_server`.
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
        return _text_result("Failed to reach the Plenum API", is_error=True)

    is_textual = any(content_type.startswith(t) for t in _TEXTUAL)
    if is_textual or not raw:
        text = raw.decode("utf-8", "replace")
    else:
        text = f"<{len(raw)} bytes of {content_type or 'binary'}; not returned over MCP — use the REST endpoint {spec.path_template}>"

    if status >= 400:
        return _text_result(f"HTTP {status}: {text}", is_error=True)
    return _text_result(text)


def build_mcp_server(
    specs: list[ToolSpec],
    session: aiohttp.ClientSession,
    base_url: str,
    internal_token: str,
) -> Server:
    """Build the low-level MCP server that serves *specs* via loopback dispatch.

    Handlers are constructor parameters in mcp v2 (the ``@server.list_tools()`` /
    ``@server.call_tool()`` decorators are gone) and must return fully-formed
    result objects — v2 no longer wraps a bare list of content blocks.
    """
    tools = [
        types.Tool(name=s.name, description=s.description, input_schema=s.input_schema)
        for s in specs
    ]
    by_name = {s.name: s for s in specs}

    async def _on_list_tools(
        _ctx: ServerRequestContext, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def _on_call_tool(
        _ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        spec = by_name.get(params.name)
        if spec is None:
            return _text_result(f"Unknown tool: {params.name}", is_error=True)
        try:
            return await dispatch_tool(
                session, base_url, spec, params.arguments or {}, internal_token
            )
        except Exception as exc:
            # v2 stops auto-wrapping handler exceptions into an error-flagged
            # result and lets them surface as a top-level JSON-RPC error. That
            # is the behaviour we want — but an exception allowed to propagate
            # raw would put str(exc) on the wire, which is exactly the
            # information disclosure CLAUDE.md forbids (CWE-209, security alert
            # #4). So: full traceback to the server log, generic message to the
            # caller. MCPError carries a structured code the client can branch
            # on without learning anything about our internals.
            log.exception(
                "MCP tool %s failed unexpectedly (%s %s)",
                params.name,
                spec.method.upper(),
                spec.path_template,
            )
            raise MCPError(types.INTERNAL_ERROR, f"Tool '{params.name}' failed to execute") from exc

    return Server("plenum", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


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
) -> ASGIApp:
    """Wrap the MCP *server* in a gated ASGI app.

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

    # Build the SDK's own Streamable HTTP app rather than mounting the legacy
    # StreamableHTTPSessionManager by hand.
    #
    # This matters for more than tidiness: only the SDK's *modern* transport
    # serves the 2026-07-28 protocol revision (mcp.client.streamable_http
    # .MODERN_PROTOCOL_VERSIONS). Driving the legacy session manager directly —
    # which is what the v1 code did, and what still imports cleanly under v2 —
    # silently caps negotiation at 2025-11-25, so the add-on would have kept
    # serving only the old protocol era while appearing to have been migrated.
    # The conformance suite's per-revision handshake is what caught that.
    #
    # DNS-rebinding protection is disabled EXPLICITLY, to preserve today's
    # behaviour rather than to weaken anything.
    #
    # `streamable_http_app` defaults `host="127.0.0.1"`, and on that default the
    # SDK auto-enables host/origin allow-listing for loopback only
    # (`127.0.0.1:*`, `localhost:*`, `[::1]:*`). Plenum binds 0.0.0.0:9099 and
    # users reach it by whatever name their network uses —
    # `homeassistant.local:9099`, a LAN IP, a reverse proxy — so inheriting that
    # default would answer 421 Misdirected Request to real clients. The v1 code
    # constructed StreamableHTTPSessionManager with no security_settings at all,
    # i.e. no host checking, so this keeps parity exactly.
    #
    # Host allow-listing would be a genuine hardening, but it needs a
    # user-configurable allowed_hosts (the add-on cannot know the deployment's
    # hostname) and is therefore its own change, not a side effect of an SDK
    # upgrade. The port stays gated by the mcp_enabled toggle and, when
    # require_auth is on, by the bearer check above.
    inner = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=stateless,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def gated(scope: Scope, receive: Receive, send: Send) -> None:
        # The inner app owns the session manager's lifespan, so pass that
        # through untouched — gating only applies to real requests.
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return

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

        await inner(scope, receive, send)

    return gated


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
) -> ASGIApp:
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
