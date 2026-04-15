"""
MCP server entry point (stdio transport).

Exposes all room/schedule/thermostat management as Claude-callable tools.
Each tool is a thin wrapper over the shared db layer.

Usage (Claude Code mcp config):
  {
    "mcpServers": {
      "flair-replacement": {
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
from mcp.server import Server
from mcp.server.stdio import stdio_server

from . import db
from .mcp_tools import ha_entities, rooms, schedules, status, thermostats

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "flair.db")


async def main() -> None:
    server = Server("flair-replacement")

    # Open a dedicated DB connection for the MCP server
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)

    # Register all tool modules
    for module in (rooms, schedules, thermostats, status, ha_entities):
        module.register(server, conn)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
