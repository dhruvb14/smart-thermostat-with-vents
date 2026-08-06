"""MCP tools: room + sensor/vent/presence management."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import aiosqlite
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

from .. import db
from ..models import Room, RoomOverride, RoomPresenceSensor, RoomSensor, RoomVent
from ..units import to_f
from ._units import active_unit, echo_abs


def register(server: MCPServer, conn: aiosqlite.Connection) -> None:

    @server.tool()
    async def list_rooms() -> list[TextContent]:
        """List all configured rooms."""
        rooms = await db.get_all_rooms(conn)
        result = []
        for r in rooms:
            sensors = await db.get_room_sensors(conn, r.id)
            vents = await db.get_room_vents(conn, r.id)
            presence = await db.get_room_presence_sensors(conn, r.id)
            result.append(
                {
                    **r.__dict__,
                    "sensor_count": len(sensors),
                    "vent_count": len(vents),
                    "presence_sensor_count": len(presence),
                }
            )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    @server.tool()
    async def get_room(room_id: str) -> list[TextContent]:
        """Get full detail for a room including sensors, vents, and schedules."""
        room = await db.get_room(conn, room_id)
        if not room:
            return [TextContent(type="text", text=f"Room {room_id} not found")]
        sensors = await db.get_room_sensors(conn, room.id)
        vents = await db.get_room_vents(conn, room.id)
        presence = await db.get_room_presence_sensors(conn, room.id)
        schedules = await db.get_schedules_for_room(conn, room.id)
        data = {
            **room.__dict__,
            "sensors": [s.__dict__ for s in sensors],
            "vents": [v.__dict__ for v in vents],
            "presence_sensors": [p.__dict__ for p in presence],
            "schedules": [
                {
                    "id": s.id,
                    # Optional label, null when unnamed (Issue #520) — a summary
                    # of a room reads better with "Night setback" than a GUID
                    # alone, and the id is right there for addressing the block.
                    "name": s.name,
                    "days_of_week": s.days_of_week,
                    "start_time": s.start_time.isoformat(),
                    "end_time": s.end_time.isoformat(),
                    "target_temp": s.target_temp,
                }
                for s in schedules
            ],
        }
        return [TextContent(type="text", text=json.dumps(data, indent=2))]

    @server.tool()
    async def create_room(
        name: str,
        thermostat_entity_id: str,
        system_wide_temp: float | None = None,
        presence_holdover_hours: float = 2.0,
        include_thermostat_sensor: bool = False,
        notes: str = "",
    ) -> list[TextContent]:
        """Create a new room.

        system_wide_temp: fixed target in the configured display unit (°C/°F),
        stored as °F. Omit to follow schedules instead of a fixed target.
        """
        unit = await active_unit(conn)
        sys_temp_f = to_f(system_wide_temp, unit) if system_wide_temp is not None else None
        room = Room.create(
            name=name,
            thermostat_entity_id=thermostat_entity_id,
            system_wide_temp=sys_temp_f,
            presence_holdover_hours=presence_holdover_hours,
            include_thermostat_sensor=include_thermostat_sensor,
            notes=notes,
        )
        await db.upsert_room(conn, room)
        suffix = (
            f" (system_wide_temp {echo_abs(sys_temp_f, unit)})" if sys_temp_f is not None else ""
        )
        return [TextContent(type="text", text=f"Created room '{name}' with id={room.id}{suffix}")]

    @server.tool()
    async def update_room(
        room_id: str,
        name: str | None = None,
        thermostat_entity_id: str | None = None,
        system_wide_temp: float | None = None,
        presence_holdover_hours: float | None = None,
        include_thermostat_sensor: bool | None = None,
        notes: str | None = None,
    ) -> list[TextContent]:
        """Update fields on an existing room.

        system_wide_temp is given in the configured display unit (°C/°F) and
        stored as °F.
        """
        room = await db.get_room(conn, room_id)
        if not room:
            return [TextContent(type="text", text=f"Room {room_id} not found")]
        if name is not None:
            room.name = name
        if thermostat_entity_id is not None:
            room.thermostat_entity_id = thermostat_entity_id
        if system_wide_temp is not None:
            room.system_wide_temp = to_f(system_wide_temp, await active_unit(conn))
        if presence_holdover_hours is not None:
            room.presence_holdover_hours = presence_holdover_hours
        if include_thermostat_sensor is not None:
            room.include_thermostat_sensor = include_thermostat_sensor
        if notes is not None:
            room.notes = notes
        await db.upsert_room(conn, room)
        return [TextContent(type="text", text=f"Updated room {room_id}")]

    @server.tool()
    async def delete_room(room_id: str) -> list[TextContent]:
        """Delete a room and all its associated data."""
        await db.delete_room(conn, room_id)
        return [TextContent(type="text", text=f"Deleted room {room_id}")]

    @server.tool()
    async def add_sensor(room_id: str, entity_id: str) -> list[TextContent]:
        """Add a temperature sensor to a room."""
        s = RoomSensor.create(room_id=room_id, entity_id=entity_id)
        await db.add_room_sensor(conn, s)
        return [TextContent(type="text", text=f"Added sensor {entity_id} to room {room_id}")]

    @server.tool()
    async def remove_sensor(room_id: str, entity_id: str) -> list[TextContent]:
        """Remove a temperature sensor from a room."""
        await db.remove_room_sensor(conn, room_id, entity_id)
        return [TextContent(type="text", text=f"Removed sensor {entity_id}")]

    @server.tool()
    async def add_vent(room_id: str, entity_id: str) -> list[TextContent]:
        """Add a Flair vent (cover entity) to a room."""
        v = RoomVent.create(room_id=room_id, entity_id=entity_id)
        await db.add_room_vent(conn, v)
        return [TextContent(type="text", text=f"Added vent {entity_id} to room {room_id}")]

    @server.tool()
    async def remove_vent(room_id: str, entity_id: str) -> list[TextContent]:
        """Remove a vent from a room."""
        await db.remove_room_vent(conn, room_id, entity_id)
        return [TextContent(type="text", text=f"Removed vent {entity_id}")]

    @server.tool()
    async def add_presence_sensor(room_id: str, entity_id: str) -> list[TextContent]:
        """Add a presence/motion sensor (binary_sensor.*) to a room."""
        p = RoomPresenceSensor.create(room_id=room_id, entity_id=entity_id)
        await db.add_room_presence_sensor(conn, p)
        return [
            TextContent(type="text", text=f"Added presence sensor {entity_id} to room {room_id}")
        ]

    @server.tool()
    async def remove_presence_sensor(room_id: str, entity_id: str) -> list[TextContent]:
        """Remove a presence sensor from a room."""
        await db.remove_room_presence_sensor(conn, room_id, entity_id)
        return [TextContent(type="text", text=f"Removed presence sensor {entity_id}")]

    @server.tool()
    async def set_room_override(
        room_id: str, target_temp: float, duration_hours: float = 2.0
    ) -> list[TextContent]:
        """Set a manual temperature override for a room.

        target_temp is given in the configured display unit (°C/°F) and stored
        as °F, matching the UI and the REST API.
        """
        unit = await active_unit(conn)
        target_f = to_f(target_temp, unit)
        override = RoomOverride(
            room_id=room_id,
            target_temp=target_f,
            expires_at=datetime.now(UTC) + timedelta(hours=duration_hours),
        )
        await db.set_room_override(conn, override)
        return [
            TextContent(
                type="text",
                text=f"Override set: room {room_id} → {echo_abs(target_f, unit)} "
                f"for {duration_hours}h (expires {override.expires_at.isoformat()})",
            )
        ]

    @server.tool()
    async def clear_room_override(room_id: str) -> list[TextContent]:
        """Clear a manual override for a room."""
        await db.clear_room_override(conn, room_id)
        return [TextContent(type="text", text=f"Override cleared for room {room_id}")]
