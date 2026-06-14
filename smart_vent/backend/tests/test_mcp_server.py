"""Smoke tests for the MCP server (Issue #282).

The MCP integration is excluded from coverage (see ``pyproject.toml``), so
nothing else imports ``mcp_server`` or actually registers the tools. The
low-level ``mcp.server.Server`` has no ``@server.tool()`` decorator — only
``FastMCP`` does — so the previous wiring raised ``AttributeError`` the moment
the first tool module registered, killing the server before it served a
request. These tests build the real server and assert every tool registers and
is callable, so that regression can't slip back in unnoticed.
"""

from __future__ import annotations

import aiosqlite
from mcp.server.fastmcp import FastMCP

from backend import db
from backend.mcp_server import build_server

# The full set of tools every tool module is expected to register. If a tool is
# added/removed/renamed, update this set deliberately.
EXPECTED_TOOLS = {
    # rooms.py
    "list_rooms",
    "get_room",
    "create_room",
    "update_room",
    "delete_room",
    "add_sensor",
    "remove_sensor",
    "add_vent",
    "remove_vent",
    "add_presence_sensor",
    "remove_presence_sensor",
    "set_room_override",
    "clear_room_override",
    # schedules.py
    "list_schedules",
    "create_schedule",
    "update_schedule",
    "delete_schedule",
    # thermostats.py
    "list_thermostat_configs",
    "set_thermostat_config",
    # status.py
    "get_system_status",
    "get_cycle_logs",
    # ha_entities.py
    "list_ha_entities",
}


class TestMcpServerBuild:
    async def test_build_server_registers_all_tools(self):
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)
        try:
            server = build_server(conn)
            assert isinstance(server, FastMCP)

            tools = await server.list_tools()
            names = {t.name for t in tools}
            assert names == EXPECTED_TOOLS

            # FastMCP auto-generates an object input schema from each tool's
            # type hints — the capability the low-level Server lacked.
            for t in tools:
                assert t.inputSchema.get("type") == "object"
        finally:
            await conn.close()

    async def test_create_argument_schema_is_generated(self):
        """create_room's schema must reflect its typed parameters."""
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)
        try:
            server = build_server(conn)
            tools = {t.name: t for t in await server.list_tools()}
            props = tools["create_room"].inputSchema.get("properties", {})
            assert "name" in props
            assert "thermostat_entity_id" in props
        finally:
            await conn.close()

    async def test_list_rooms_tool_is_callable(self):
        """A read tool round-trips through FastMCP.call_tool and returns text."""
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)
        try:
            server = build_server(conn)
            content, _structured = await server.call_tool("list_rooms", {})
            assert content
            assert content[0].type == "text"
            # Empty DB → an empty JSON array of rooms.
            assert content[0].text.strip() == "[]"
        finally:
            await conn.close()
