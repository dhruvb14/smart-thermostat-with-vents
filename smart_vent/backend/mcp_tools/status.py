"""MCP tools: system status (read-only)."""

from __future__ import annotations

import json

import aiosqlite
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

from .. import db


def register(server: MCPServer, conn: aiosqlite.Connection) -> None:

    @server.tool()
    async def get_system_status() -> list[TextContent]:
        """
        Get a summary of all rooms, their current schedules, overrides,
        and presence holdover states.
        Note: live cycle state (temperatures, vent positions) is only
        available via the REST API /api/status when the add-on is running.
        This tool reads persisted DB state only.
        """
        rooms = await db.get_all_rooms(conn)
        result = []
        for room in rooms:
            override = await db.get_room_override(conn, room.id)
            holdover = await db.get_holdover_state(conn, room.id)
            schedules = await db.get_schedules_for_room(conn, room.id)
            result.append(
                {
                    "room_id": room.id,
                    "name": room.name,
                    "thermostat_entity_id": room.thermostat_entity_id,
                    "system_wide_temp": room.system_wide_temp,
                    "presence_holdover_hours": room.presence_holdover_hours,
                    "active_override": {
                        "target_temp": override.target_temp,
                        "expires_at": override.expires_at.isoformat(),
                        "respect_eco": override.respect_eco,
                    }
                    if override
                    else None,
                    "presence_holdover": {
                        "last_detected_at": holdover.last_detected_at.isoformat(),
                        "expires_at": holdover.expires_at.isoformat(),
                    }
                    if holdover
                    else None,
                    "schedule_count": len(schedules),
                }
            )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    @server.tool()
    async def get_cycle_logs(limit: int = 10) -> list[TextContent]:
        """Get recent HVAC cycle logs."""
        logs = await db.get_cycle_logs(conn, limit=limit)
        data = [
            {
                "id": log_entry.id,
                "thermostat_entity_id": log_entry.thermostat_entity_id,
                "started_at": log_entry.started_at.isoformat(),
                "ended_at": log_entry.ended_at.isoformat() if log_entry.ended_at else None,
                "mode": log_entry.mode,
                "rooms": json.loads(log_entry.rooms_json),
            }
            for log_entry in logs
        ]
        return [TextContent(type="text", text=json.dumps(data, indent=2))]
