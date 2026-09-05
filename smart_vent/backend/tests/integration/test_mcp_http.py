"""Integration tests for the HTTP MCP server (Issue #372).

Exercises the whole path: the low-level MCP server, the Streamable HTTP
transport, and loopback dispatch back through the real REST API.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable
from typing import Any

import aiohttp
import pytest
import uvicorn
from aiohttp.test_utils import TestClient
from starlette.testclient import TestClient as StarletteTestClient

from backend.mcp_http import build_asgi_app, build_mcp_asgi_app, build_mcp_server, dispatch_tool
from backend.mcp_openapi import build_tool_specs


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _spec(client, name):
    return next(s for s in build_tool_specs(client.app) if s.name == name)


def _base_url(client) -> str:
    return str(client.make_url(""))


def _tok(client) -> str:
    # The per-process CSRF loopback token build_app minted for this app.
    return str(client.app["internal_token"])


async def test_dispatch_success_error_and_roundtrip(client: TestClient) -> None:
    base = _base_url(client)
    tok = _tok(client)
    async with aiohttp.ClientSession() as session:
        # 2xx textual → a non-error CallToolResult carrying TextContent.
        ok: Any = await dispatch_tool(session, base, _spec(client, "get_healthz"), {}, tok)
        assert ok.is_error is False
        assert json.loads(ok.content[0].text) == {"ok": True}

        # write then read back through the loopback (proves it hits real routes)
        await dispatch_tool(
            session,
            base,
            _spec(client, "post_rooms"),
            {"name": "Office", "thermostat_entity_id": "climate.office"},
            tok,
        )
        rooms: Any = await dispatch_tool(session, base, _spec(client, "get_rooms"), {}, tok)
        assert "Office" in rooms.content[0].text

        # 4xx → CallToolResult flagged is_error, echoing the safe message
        err: Any = await dispatch_tool(
            session, base, _spec(client, "get_rooms_room_id"), {"room_id": "missing"}, tok
        )
        assert err.is_error is True
        assert "HTTP 404" in err.content[0].text


async def test_eco_impact_tools_exposed_over_mcp(client: TestClient) -> None:
    """The Eco Mode impact endpoints (Issue #404) auto-surface as MCP tools and
    dispatch through the real routes for post-rollout trend analysis."""
    names = {s.name for s in build_tool_specs(client.app)}
    assert "get_metrics_thermostats_entity_id_eco_impact" in names
    assert "get_metrics_thermostats_eco_impact" in names

    base = _base_url(client)
    async with aiohttp.ClientSession() as session:
        result: Any = await dispatch_tool(
            session, base, _spec(client, "get_metrics_thermostats_eco_impact"), {}, _tok(client)
        )
        payload = json.loads(result.content[0].text)
        assert "eco_active_cycles" in payload
        assert "avg_drift_f" in payload
        assert "rooms" in payload


async def test_malformed_metrics_date_reaches_mcp_as_an_actionable_400(
    client: TestClient,
) -> None:
    """#606's headline victim. ``build_tool_specs`` auto-generates the metrics
    summary tools with free-form ``start``/``end`` strings whose ``format:
    "date"`` annotation is documentation-only, so an assistant asked "how many
    hours did the system run in June" quite reasonably emits ``2025-6-1``. That
    used to be an opaque ``HTTP 500: Internal Server Error`` (or, on the nine
    non-summary tools, a confident zero). It is now a 400 whose body is the same
    ``{"error": ...}`` JSON every other Plenum 400 returns, naming the knob and
    the expected format — enough for the caller to retry correctly."""
    base = _base_url(client)
    async with aiohttp.ClientSession() as session:
        result: Any = await dispatch_tool(
            session,
            base,
            _spec(client, "get_metrics_thermostats_summary"),
            {"end": "2025-6-1"},
            _tok(client),
        )
        assert result.is_error is True
        text = result.content[0].text
        assert text == 'HTTP 400: {"error": "end must be an ISO date (YYYY-MM-DD)"}'
        # CWE-209: no exception text crosses the MCP boundary either.
        assert "isoformat" not in text
        assert "Traceback" not in text


async def test_dispatch_binary_endpoint_is_summarised(client: TestClient) -> None:
    base = _base_url(client)
    async with aiohttp.ClientSession() as session:
        result: Any = await dispatch_tool(
            session, base, _spec(client, "get_backup"), {}, _tok(client)
        )
        assert result.is_error is False
        # The DB backup is binary — not decoded, just described.
        assert "use the REST endpoint" in result.content[0].text


async def test_dispatch_unreachable_api_returns_safe_error(client: TestClient) -> None:
    # Point at a closed port so the loopback call fails.
    async with aiohttp.ClientSession() as session:
        result: Any = await dispatch_tool(
            session, "http://127.0.0.1:1", _spec(client, "get_healthz"), {}, "unused"
        )
        assert result.is_error is True
        assert "Failed to reach" in result.content[0].text


async def test_asgi_returns_503_when_disabled() -> None:
    async with aiohttp.ClientSession() as session:
        server = build_mcp_server([], session, "http://127.0.0.1:1", "unused")
        app = build_asgi_app(server, is_enabled=lambda: False)
        with StarletteTestClient(app) as tc:
            resp = tc.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert resp.status_code == 503
            assert "disabled" in resp.json()["error"]


# --------------------------------------------------------------------------
# Bearer validation at the 9099 ASGI layer (Issue #373, Phase 4)
# --------------------------------------------------------------------------


async def test_extract_bearer_parses_authorization_header() -> None:
    from backend.mcp_http import _extract_bearer

    assert _extract_bearer({"headers": [(b"authorization", b"Bearer abc123")]}) == "abc123"
    assert _extract_bearer({"headers": [(b"authorization", b"bearer xyz")]}) == "xyz"
    assert _extract_bearer({"headers": [(b"authorization", b"Basic zzz")]}) is None
    assert _extract_bearer({"headers": []}) is None
    assert _extract_bearer({"headers": [(b"authorization", b"Bearer   ")]}) is None


def _post(tc, headers=None):
    # One StreamableHTTPSessionManager per app can only .run() once, so all
    # requests in a test share a single StarletteTestClient (posts are fine;
    # only lifespan re-entry is not).
    return tc.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        headers={"Accept": "application/json, text/event-stream", **(headers or {})},
    )


async def test_mcp_requires_bearer_when_auth_on() -> None:
    async def validate(token: str):
        return "read" if token == "good" else None

    async with aiohttp.ClientSession() as session:
        server = build_mcp_server([], session, "http://127.0.0.1:1", "unused")
        app = build_asgi_app(
            server, is_enabled=lambda: True, require_auth=lambda: True, validate_bearer=validate
        )
        with StarletteTestClient(app) as tc:
            assert _post(tc).status_code == 401  # no Authorization
            assert _post(tc, {"Authorization": "Bearer bad"}).status_code == 401
            # Valid token → auth passes (proceeds into the session manager, not 401).
            assert _post(tc, {"Authorization": "Bearer good"}).status_code != 401


async def test_mcp_open_when_auth_off() -> None:
    async with aiohttp.ClientSession() as session:
        server = build_mcp_server([], session, "http://127.0.0.1:1", "unused")
        app = build_asgi_app(
            server, is_enabled=lambda: True, require_auth=lambda: False, validate_bearer=None
        )
        with StarletteTestClient(app) as tc:
            # Legacy open mode: no bearer needed, request is not 401.
            assert _post(tc).status_code != 401


async def test_mcp_503_takes_precedence_over_bearer() -> None:
    async with aiohttp.ClientSession() as session:
        server = build_mcp_server([], session, "http://127.0.0.1:1", "unused")
        app = build_asgi_app(
            server, is_enabled=lambda: False, require_auth=lambda: True, validate_bearer=None
        )
        with StarletteTestClient(app) as tc:
            # Disabled → 503 regardless of auth (the toggle wins over the token gate).
            assert _post(tc).status_code == 503


async def test_full_stack_over_the_wire(client: TestClient) -> None:
    from mcp import Client

    mcp_port = _free_port()
    async with aiohttp.ClientSession() as session:
        asgi = build_mcp_asgi_app(
            client.app,
            session,
            _base_url(client),
            is_enabled=lambda: True,
            internal_token=_tok(client),
        )
        config = uvicorn.Config(
            asgi, host="127.0.0.1", port=mcp_port, log_level="error", lifespan="on"
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # type: ignore[attr-defined]
        task = asyncio.create_task(server.serve())
        try:
            while not server.started:  # noqa: ASYNC110
                await asyncio.sleep(0.05)

            url = f"http://127.0.0.1:{mcp_port}/mcp"
            # mcp v2 replaces the streamablehttp_client + ClientSession pair
            # with a single first-class Client that connects and negotiates on
            # entry.
            async with Client(url) as cs:
                tools = await cs.list_tools()
                assert len(tools.tools) > 50
                assert "get_healthz" in {t.name for t in tools.tools}

                ok: Any = await cs.call_tool("get_healthz", {})
                assert json.loads(ok.content[0].text) == {"ok": True}

                # Unknown tool → server-side error-flagged result.
                bad = await cs.call_tool("does_not_exist", {})
                assert bad.is_error is True
        finally:
            server.should_exit = True
            await task


async def test_full_stack_scope_enforced_end_to_end(make_client: Callable) -> None:
    """The definitive #373 Phase-4 proof: over a REAL MCP client → server →
    loopback REST path, with require_auth ON, a read-scoped token can call a read
    tool but is forbidden from a write tool — confirming the granted scope
    propagates from handle_mcp, through the session manager, into dispatch_tool's
    X-Plenum-Scope header, and is enforced at the REST boundary."""
    from backend import session as _session
    from backend.main import validate_mcp_bearer

    from .mcp_wire import PROTOCOL_REVISIONS, RawMcpClient, tool_failed, tool_result_text

    client = await make_client(require_auth=True)  # REST app enforces auth
    scheduler = client.app["scheduler"]
    # A logged-in web admin (session cookie = full access) mints a READ token.
    admin = {_session.COOKIE_NAME: _session.issue_token(client.app["session_secret"], "admin")}
    minted = await client.post(
        "/api/mcp/tokens", json={"label": "ro", "scope": "read"}, cookies=admin
    )
    raw = (await minted.json())["token"]

    mcp_port = _free_port()
    async with aiohttp.ClientSession() as session:
        asgi = build_mcp_asgi_app(
            client.app,
            session,
            _base_url(client),
            is_enabled=lambda: True,
            internal_token=_tok(client),
            require_auth=lambda: True,
            validate_bearer=lambda t: validate_mcp_bearer(scheduler, t),
        )
        config = uvicorn.Config(
            asgi, host="127.0.0.1", port=mcp_port, log_level="error", lifespan="on"
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # type: ignore[attr-defined]
        task = asyncio.create_task(server.serve())
        try:
            while not server.started:  # noqa: ASYNC110
                await asyncio.sleep(0.05)

            url = f"http://127.0.0.1:{mcp_port}/mcp"
            # Driven by the conformance suite's raw-wire client rather than the
            # SDK's: mcp v2 routes custom headers through an httpx2 client that
            # does not compose with `Client(Transport)`, and a bearer token is
            # the entire point of this test. The wire driver is also
            # SDK-version-agnostic, so this assertion is now identical before
            # and after the migration.
            cs = RawMcpClient(session, url, bearer=raw)
            await cs.initialize(PROTOCOL_REVISIONS[-1])

            # read tool + read token → allowed
            rooms = await cs.call_tool_raw("get_rooms", {})
            assert not tool_failed(rooms)

            # write tool + read token → forbidden at the REST boundary (403)
            denied = await cs.call_tool_raw(
                "post_rooms", {"name": "Nope", "thermostat_entity_id": "climate.nope"}
            )
            assert tool_failed(denied)
            assert "403" in tool_result_text(denied)
        finally:
            server.should_exit = True
            await task


@pytest.mark.parametrize("enabled", [True, False])
async def test_build_mcp_asgi_app_wires_enable_flag(client: TestClient, enabled: bool) -> None:
    async with aiohttp.ClientSession() as session:
        app = build_mcp_asgi_app(
            client.app,
            session,
            _base_url(client),
            is_enabled=lambda: enabled,
            internal_token=_tok(client),
        )
        # The /mcp endpoint exists either way; the flag decides whether it
        # answers (503) rather than whether it is mounted.
        with StarletteTestClient(app) as tc:
            resp = tc.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert (resp.status_code == 503) is (not enabled)


# ---------------------------------------------------------------------------
# Transport mode (Issue #543)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, True),  # unset → production default: stateless
        ("true", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("  False  ", False),  # tolerate whitespace/case from compose files
    ],
)
def test_stateless_from_env(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool
) -> None:
    """PLENUM_MCP_STATELESS resolves to the transport mode, defaulting to stateless.

    The conformance suite's stateful axis is only meaningful if this switch is
    actually honoured, so pin every spelling CI might set.
    """
    from backend.mcp_http import _stateless_from_env

    if raw is None:
        monkeypatch.delenv("PLENUM_MCP_STATELESS", raising=False)
    else:
        monkeypatch.setenv("PLENUM_MCP_STATELESS", raw)
    assert _stateless_from_env() is expected


@pytest.mark.parametrize("stateless", [True, False])
async def test_build_asgi_app_honours_stateless_argument(
    client: TestClient, stateless: bool
) -> None:
    """An explicit stateless= argument reaches the SDK's session manager."""
    from backend.mcp_http import build_asgi_app, build_mcp_server
    from backend.mcp_openapi import build_tool_specs

    async with aiohttp.ClientSession() as session:
        server = build_mcp_server(
            build_tool_specs(client.app), session, _base_url(client), _tok(client)
        )
        app = build_asgi_app(server, lambda: True, stateless=stateless)
        # Behaviour, not structure: a stateful transport issues an
        # Mcp-Session-Id on the handshake and a stateless one never does.
        with StarletteTestClient(app) as tc:
            resp = tc.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                },
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert resp.status_code == 200
        assert bool(resp.headers.get("mcp-session-id")) is (not stateless)


async def test_build_asgi_app_defaults_stateless_from_env(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no explicit argument the env var decides (stateful when told to)."""
    from backend.mcp_http import build_asgi_app, build_mcp_server
    from backend.mcp_openapi import build_tool_specs

    monkeypatch.setenv("PLENUM_MCP_STATELESS", "false")
    async with aiohttp.ClientSession() as session:
        server = build_mcp_server(
            build_tool_specs(client.app), session, _base_url(client), _tok(client)
        )
        app = build_asgi_app(server, lambda: True)
        # No explicit argument → the env var decides, so a session id appears.
        with StarletteTestClient(app) as tc:
            resp = tc.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                },
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert resp.status_code == 200
        assert resp.headers.get("mcp-session-id"), (
            "PLENUM_MCP_STATELESS=false should make the transport stateful"
        )


# ---------------------------------------------------------------------------
# Unexpected-exception semantics under mcp v2 (Issue #543)
# ---------------------------------------------------------------------------


async def test_unexpected_tool_exception_is_scrubbed_on_the_wire(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected handler exception surfaces as a JSON-RPC error, scrubbed.

    mcp v2 no longer auto-wraps handler exceptions into an error-flagged tool
    result; they propagate as top-level JSON-RPC errors. That is the behaviour
    we want, but a raw propagation would put ``str(exc)`` on the wire — the
    exact information disclosure CLAUDE.md forbids (CWE-209, security alert #4).

    Driven over the real transport rather than by poking the handler, so what is
    asserted is what a client would actually receive.
    """
    from backend import mcp_http

    from .mcp_wire import PROTOCOL_REVISIONS, RawMcpClient

    secret = "SECRET /etc/passwd postgres://user:pw@host/db"

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(secret)

    # _on_call_tool resolves dispatch_tool from module globals at call time,
    # so patching the module attribute reaches the running server.
    monkeypatch.setattr(mcp_http, "dispatch_tool", _boom)

    mcp_port = _free_port()
    async with aiohttp.ClientSession() as session:
        asgi = build_mcp_asgi_app(
            client.app,
            session,
            _base_url(client),
            is_enabled=lambda: True,
            internal_token=_tok(client),
        )
        config = uvicorn.Config(
            asgi, host="127.0.0.1", port=mcp_port, log_level="error", lifespan="on"
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # type: ignore[attr-defined]
        task = asyncio.create_task(server.serve())
        try:
            while not server.started:  # noqa: ASYNC110
                await asyncio.sleep(0.05)

            cs = RawMcpClient(session, f"http://127.0.0.1:{mcp_port}/mcp")
            await cs.initialize(PROTOCOL_REVISIONS[-1])
            reply = await cs.call_tool_raw("get_healthz", {})
        finally:
            server.should_exit = True
            await task

    blob = json.dumps(reply)
    # It failed, and it failed as a protocol-level error (v2 semantics).
    assert "error" in reply, f"expected a JSON-RPC error member, got: {blob[:300]}"
    assert reply["error"]["code"] == -32603  # INTERNAL_ERROR

    # And it leaked nothing.
    for leak in (secret, "postgres://", "/etc/passwd", "Traceback", "RuntimeError"):
        assert leak not in blob, f"error payload leaked {leak!r}: {blob[:400]}"
    assert "get_healthz" in blob, "the safe message should still name the tool"
