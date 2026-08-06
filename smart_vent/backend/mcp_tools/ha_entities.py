"""MCP tools: HA entity discovery (requires live HA connection via REST)."""

from __future__ import annotations

import json
import os
import ssl

import aiohttp
import aiosqlite

try:
    import certifi

    _SSL_CONTEXT: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent


def register(server: MCPServer, conn: aiosqlite.Connection) -> None:

    @server.tool()
    async def list_ha_entities(domain: str) -> list[TextContent]:
        """
        List available Home Assistant entities for a given domain.
        Useful for discovering entity IDs when configuring rooms.

        Common domains: sensor, climate, cover, binary_sensor
        """
        ha_url = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
        token = os.environ.get("HA_TOKEN", "")
        headers = {"Authorization": f"Bearer {token}"}

        try:
            connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT)
            async with (
                aiohttp.ClientSession(connector=connector) as session,
                session.get(f"{ha_url}/api/states", headers=headers) as resp,
            ):
                resp.raise_for_status()
                states = await resp.json()
        except Exception as exc:
            return [TextContent(type="text", text=f"Error fetching HA entities: {exc}")]

        filtered = [
            {
                "entity_id": s["entity_id"],
                "state": s.get("state"),
                "friendly_name": s.get("attributes", {}).get("friendly_name", s["entity_id"]),
            }
            for s in states
            if s["entity_id"].startswith(f"{domain}.")
        ]
        filtered.sort(key=lambda x: x["entity_id"])
        return [
            TextContent(
                type="text",
                text=f"Found {len(filtered)} entities in domain '{domain}':\n"
                + json.dumps(filtered, indent=2),
            )
        ]
