"""MCP tools: system status (read-only)."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

from .. import db


def _decode_rooms(rooms_json: str | None) -> Any:
    """Decode a ``cycle_logs.rooms_json`` snapshot, degrading to ``{}``.

    #604: the column is only ever written by us as a dict of per-room dicts,
    but a hand-edited backup uploaded to /api/restore — or on-disk corruption
    that happens to leave a truncated value — can put something unparseable
    there. A bare ``json.loads`` turned one such row into a tool call that
    failed outright, so the whole cycle-log listing was lost rather than one
    entry's room detail.

    Only the decode is guarded here, deliberately: nothing in this tool walks
    the snapshot (it is re-serialised into the tool result as-is), so a value
    that parses but has the wrong *shape* is harmless — unlike the two sites
    that iterate it, ``CycleEngine.restore_from_db`` and
    ``db.compute_thermostat_summary``.
    """
    try:
        return json.loads(rooms_json or "{}")
    except (ValueError, TypeError):
        return {}


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
                "rooms": _decode_rooms(log_entry.rooms_json),
            }
            for log_entry in logs
        ]
        return [TextContent(type="text", text=json.dumps(data, indent=2))]
