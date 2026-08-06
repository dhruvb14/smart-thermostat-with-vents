"""
MCP server entry point (stdio transport).

Exposes all room/schedule/thermostat management as Claude-callable tools.
Each tool is a thin wrapper over the shared db layer.

Usage (Claude Code mcp config):
  {
    "mcpServers": {
      "plenum": {
        "command": "python",
        "args": ["-m", "backend.mcp_server"],
        "env": {
          "HA_URL": "http://homeassistant.local:8123",
          "HA_TOKEN": "<long-lived-access-token>",
          "DATA_DIR": "/path/to/data"
        }
      }
    }
  }
"""

from __future__ import annotations

import asyncio
import os

import aiosqlite
from mcp.server.mcpserver import MCPServer

from . import db
from .main import _migrate_db_filename
from .mcp_tools import ha_entities, rooms, schedules, status, thermostats

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "app.db")


def build_server(conn: aiosqlite.Connection) -> MCPServer:
    """Create the MCPServer and register every tool module against *conn*.

    Split out from :func:`main` so tests can build and introspect the server
    (e.g. assert the tools registered) without standing up the stdio transport.
    """
    # MCPServer (mcp v2's rename of FastMCP) — not the low-level Server — owns
    # the `@server.tool()` decorator that auto-generates each tool's JSON schema
    # from its type hints. The low-level Server has no `.tool()` and raised
    # AttributeError at startup, killing the whole MCP integration before it
    # served a request. (Issue #282)
    mcp = MCPServer("plenum")
    for module in (rooms, schedules, thermostats, status, ha_entities):
        module.register(mcp, conn)
    return mcp


async def main() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    _migrate_db_filename(DATA_DIR)

    # Open a dedicated DB connection for the MCP server
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)

    mcp = build_server(conn)
    try:
        await mcp.run_stdio_async()
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
