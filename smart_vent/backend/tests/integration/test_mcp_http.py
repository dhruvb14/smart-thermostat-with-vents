"""Integration tests for the HTTP MCP server (Issue #372).

Exercises the whole path: the low-level MCP server, the Streamable HTTP
transport, and loopback dispatch back through the real REST API.
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import aiohttp
import pytest
import uvicorn
from aiohttp.test_utils import TestClient

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
        # 2xx textual → list of TextContent
        ok: Any = await dispatch_tool(session, base, _spec(client, "get_healthz"), {}, tok)
        assert isinstance(ok, list)
        assert json.loads(ok[0].text) == {"ok": True}

        # write then read back through the loopback (proves it hits real routes)
        await dispatch_tool(
            session,
            base,
            _spec(client, "post_rooms"),
            {"name": "Office", "thermostat_entity_id": "climate.office"},
            tok,
        )
        rooms: Any = await dispatch_tool(session, base, _spec(client, "get_rooms"), {}, tok)
        assert "Office" in rooms[0].text

        # 4xx → CallToolResult flagged isError, echoing the safe message
        err: Any = await dispatch_tool(
            session, base, _spec(client, "get_rooms_room_id"), {"room_id": "missing"}, tok
        )
        assert err.isError is True
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
        payload = json.loads(result[0].text)
        assert "eco_active_cycles" in payload
        assert "avg_drift_f" in payload
        assert "rooms" in payload


async def test_dispatch_binary_endpoint_is_summarised(client: TestClient) -> None:
    base = _base_url(client)
    async with aiohttp.ClientSession() as session:
        result: Any = await dispatch_tool(
            session, base, _spec(client, "get_backup"), {}, _tok(client)
        )
        assert isinstance(result, list)
        # The DB backup is binary — not decoded, just described.
        assert "use the REST endpoint" in result[0].text


async def test_dispatch_unreachable_api_returns_safe_error(client: TestClient) -> None:
    # Point at a closed port so the loopback call fails.
    async with aiohttp.ClientSession() as session:
        result: Any = await dispatch_tool(
            session, "http://127.0.0.1:1", _spec(client, "get_healthz"), {}, "unused"
        )
        assert result.isError is True
        assert "Failed to reach" in result.content[0].text


async def test_asgi_returns_503_when_disabled() -> None:
    from starlette.testclient import TestClient as StarletteTestClient

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


async def test_full_stack_over_the_wire(client: TestClient) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

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
            async with (
                streamablehttp_client(url) as (read, write, _),
                ClientSession(read, write) as cs,
            ):
                await cs.initialize()
                tools = await cs.list_tools()
                assert len(tools.tools) > 50
                assert "get_healthz" in {t.name for t in tools.tools}

                ok: Any = await cs.call_tool("get_healthz", {})
                assert json.loads(ok.content[0].text) == {"ok": True}

                # Unknown tool → server-side isError result.
                bad = await cs.call_tool("does_not_exist", {})
                assert bad.isError is True
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
        # The Starlette app mounts a single /mcp route regardless of the flag.
        assert any(getattr(r, "path", "") == "/mcp" for r in app.routes)
