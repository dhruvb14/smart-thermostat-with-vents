"""MCP tools: schedule management."""

from __future__ import annotations

import json
from datetime import time

import aiosqlite
from mcp.server import Server
from mcp.types import TextContent

from .. import db
from ..models import Schedule


def register(server: Server, conn: aiosqlite.Connection) -> None:
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
    ) -> list[TextContent]:
        """
        Create a schedule block for a room.
        days_of_week: list of ints 0–6 (0=Monday, 6=Sunday)
        start_time / end_time: 'HH:MM' format
        target_temp: target temperature in °F
        """
        s = Schedule.create(
            room_id=room_id,
            days_of_week=days_of_week,
            start_time=time.fromisoformat(start_time),
            end_time=time.fromisoformat(end_time),
            target_temp=target_temp,
        )
        await db.upsert_schedule(conn, s)
        return [
            TextContent(
                type="text",
                text=f"Created schedule {s.id} for room {room_id}: "
                f"days={days_of_week} {start_time}–{end_time} @ {target_temp}°F",
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
    ) -> list[TextContent]:
        """Update an existing schedule block. room_id is required to locate the schedule."""
        schedules = await db.get_schedules_for_room(conn, room_id)
        s = next((x for x in schedules if x.id == schedule_id), None)
        if not s:
            return [TextContent(type="text", text=f"Schedule {schedule_id} not found")]
        if days_of_week is not None:
            s.days_of_week = days_of_week
        if start_time is not None:
            s.start_time = time.fromisoformat(start_time)
        if end_time is not None:
            s.end_time = time.fromisoformat(end_time)
        if target_temp is not None:
            s.target_temp = target_temp
        await db.upsert_schedule(conn, s)
        return [TextContent(type="text", text=f"Updated schedule {schedule_id}")]

    @server.tool()
    async def delete_schedule(schedule_id: str) -> list[TextContent]:
        """Delete a schedule block by ID."""
        await db.delete_schedule(conn, schedule_id)
        return [TextContent(type="text", text=f"Deleted schedule {schedule_id}")]
