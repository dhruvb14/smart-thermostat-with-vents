"""MCP tools: schedule management."""

from __future__ import annotations

import json
from datetime import time

import aiosqlite
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

from .. import db
from ..models import Schedule
from ..units import delta_to_f, to_f
from ._units import active_unit, echo_abs, echo_delta


def _validate_deadband_override(value: float, unit: str) -> tuple[str | None, float]:
    """Convert and bound an MCP-supplied schedule deadband (Issue #517).

    Mirrors ``routes._validate_deadband_override``: a DELTA (no -32 offset)
    converted via ``delta_to_f`` and bounded to 0–10 °F. The REST boundary
    validates; the MCP boundary must too, or a client can persist a band the UI
    would have rejected. Returns ``(error_message | None, value_f)``.
    """
    value_f = delta_to_f(value, unit)
    if not (0 <= value_f <= 10):
        return "deadband_override must be between 0 and 10°F (or equivalent)", value_f
    return None, value_f


def register(server: FastMCP, conn: aiosqlite.Connection) -> None:

    @server.tool()
    async def list_schedules(room_id: str) -> list[TextContent]:
        """List all schedule blocks for a room."""
        schedules = await db.get_schedules_for_room(conn, room_id)
        data = [
            {
                "id": s.id,
                "room_id": s.room_id,
                "days_of_week": s.days_of_week,
                "start_time": s.start_time.isoformat(),
                "end_time": s.end_time.isoformat(),
                "target_temp": s.target_temp,
                "enabled": s.enabled,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                "deadband_override": s.deadband_override,
            }
            for s in schedules
        ]
        return [TextContent(type="text", text=json.dumps(data, indent=2))]

    @server.tool()
    async def create_schedule(
        room_id: str,
        days_of_week: list[int],
        start_time: str,
        end_time: str,
        target_temp: float,
        deadband_override: float | None = None,
    ) -> list[TextContent]:
        """
        Create a schedule block for a room.
        days_of_week: list of ints 0–6 (0=Monday, 6=Sunday)
        start_time / end_time: 'HH:MM' format
        target_temp: target temperature in the configured display unit (°C/°F),
          stored as °F (same convention as the UI and the REST API).
        deadband_override: optional tolerance band for THIS block, as a DELTA in
          the configured display unit (0–10°F or equivalent). While the block is
          running, the room may drift this far from target before calling for
          heating or cooling — a wider band saves runtime in a room nobody is
          using. Omit or pass null to inherit the room's deadband override, then
          the thermostat's deadband.
        """
        unit = await active_unit(conn)
        target_f = to_f(target_temp, unit)
        deadband_f: float | None = None
        if deadband_override is not None:
            err, deadband_f = _validate_deadband_override(deadband_override, unit)
            if err:
                return [TextContent(type="text", text=err)]
        s = Schedule.create(
            room_id=room_id,
            days_of_week=days_of_week,
            start_time=time.fromisoformat(start_time),
            end_time=time.fromisoformat(end_time),
            target_temp=target_f,
            deadband_override=deadband_f,
        )
        await db.upsert_schedule(conn, s)
        band = "" if deadband_f is None else f", drift ±{echo_delta(deadband_f, unit)}"
        return [
            TextContent(
                type="text",
                text=f"Created schedule {s.id} for room {room_id}: "
                f"days={days_of_week} {start_time}–{end_time} @ {echo_abs(target_f, unit)}{band}",
            )
        ]

    @server.tool()
    async def update_schedule(
        schedule_id: str,
        room_id: str,
        days_of_week: list[int] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        target_temp: float | None = None,
        deadband_override: float | None = None,
        clear_deadband_override: bool = False,
    ) -> list[TextContent]:
        """Update an existing schedule block. room_id is required to locate the schedule.

        target_temp is given in the configured display unit (°C/°F), stored as °F.

        deadband_override sets this block's drift tolerance, as a DELTA in the
        configured display unit (0–10°F or equivalent). Omitting it leaves the
        current value alone; because omitted and null are indistinguishable
        here, pass clear_deadband_override=true to REMOVE the override and go
        back to inheriting the room's, then the thermostat's, deadband.
        """
        schedules = await db.get_schedules_for_room(conn, room_id)
        s = next((x for x in schedules if x.id == schedule_id), None)
        if not s:
            return [TextContent(type="text", text=f"Schedule {schedule_id} not found")]
        if deadband_override is not None and clear_deadband_override:
            return [
                TextContent(
                    type="text",
                    text="Pass either deadband_override or clear_deadband_override, not both",
                )
            ]
        if days_of_week is not None:
            s.days_of_week = days_of_week
        if start_time is not None:
            s.start_time = time.fromisoformat(start_time)
        if end_time is not None:
            s.end_time = time.fromisoformat(end_time)
        if target_temp is not None:
            s.target_temp = to_f(target_temp, await active_unit(conn))
        if clear_deadband_override:
            s.deadband_override = None
        elif deadband_override is not None:
            err, deadband_f = _validate_deadband_override(
                deadband_override, await active_unit(conn)
            )
            if err:
                return [TextContent(type="text", text=err)]
            s.deadband_override = deadband_f
        await db.upsert_schedule(conn, s)
        return [TextContent(type="text", text=f"Updated schedule {schedule_id}")]

    @server.tool()
    async def delete_schedule(schedule_id: str) -> list[TextContent]:
        """Delete a schedule block by ID."""
        await db.delete_schedule(conn, schedule_id)
        return [TextContent(type="text", text=f"Deleted schedule {schedule_id}")]
