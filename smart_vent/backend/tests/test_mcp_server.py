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

import json
from datetime import time

import aiosqlite
from mcp.server.fastmcp import FastMCP

from backend import db
from backend.mcp_server import build_server
from backend.models import Room, Schedule

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


# ---------------------------------------------------------------------------
# Issue #284: MCP write tools must convert temperature inputs from the active
# display unit to °F storage, mirroring the REST write boundary. The MCP process
# has no Scheduler, so the unit comes from system_settings.temperature_unit.
# ---------------------------------------------------------------------------


async def _conn_with_unit(unit: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    await db.set_system_setting(conn, "temperature_unit", unit)
    return conn


async def _seed_room(conn: aiosqlite.Connection) -> Room:
    room = Room.create(name="Bedroom", thermostat_entity_id="climate.x")
    await db.upsert_room(conn, room)
    return room


class TestMcpTemperatureConversion:
    async def test_create_room_converts_system_wide_temp_from_celsius(self):
        conn = await _conn_with_unit("C")
        try:
            server = build_server(conn)
            await server.call_tool(
                "create_room",
                {"name": "Bedroom", "thermostat_entity_id": "climate.x", "system_wide_temp": 21.0},
            )
            rooms = await db.get_all_rooms(conn)
            assert len(rooms) == 1
            assert rooms[0].system_wide_temp == 69.8  # 21°C → 69.8°F
        finally:
            await conn.close()

    async def test_create_room_fahrenheit_is_identity(self):
        conn = await _conn_with_unit("F")
        try:
            server = build_server(conn)
            await server.call_tool(
                "create_room",
                {"name": "Den", "thermostat_entity_id": "climate.x", "system_wide_temp": 70.0},
            )
            rooms = await db.get_all_rooms(conn)
            assert rooms[0].system_wide_temp == 70.0
        finally:
            await conn.close()

    async def test_update_room_converts_system_wide_temp_from_celsius(self):
        conn = await _conn_with_unit("C")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await server.call_tool("update_room", {"room_id": room.id, "system_wide_temp": 21.0})
            updated = await db.get_room(conn, room.id)
            assert updated is not None
            assert updated.system_wide_temp == 69.8
        finally:
            await conn.close()

    async def test_set_room_override_converts_target_from_celsius(self):
        conn = await _conn_with_unit("C")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await server.call_tool("set_room_override", {"room_id": room.id, "target_temp": 21.0})
            ov = await db.get_room_override(conn, room.id)
            assert ov is not None
            assert ov.target_temp == 69.8
        finally:
            await conn.close()

    async def test_create_schedule_converts_target_from_celsius(self):
        conn = await _conn_with_unit("C")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await server.call_tool(
                "create_schedule",
                {
                    "room_id": room.id,
                    "days_of_week": [0, 1, 2],
                    "start_time": "08:00",
                    "end_time": "22:00",
                    "target_temp": 21.0,
                },
            )
            scheds = await db.get_schedules_for_room(conn, room.id)
            assert len(scheds) == 1
            assert scheds[0].target_temp == 69.8
        finally:
            await conn.close()

    async def test_set_thermostat_config_converts_absolute_and_delta(self):
        conn = await _conn_with_unit("C")
        try:
            server = build_server(conn)
            await server.call_tool(
                "set_thermostat_config",
                {
                    "thermostat_entity_id": "climate.x",
                    "min_setpoint": 16.0,
                    "max_setpoint": 27.0,
                    "deadband": 1.0,
                    "overshoot_delta": 2.0,
                },
            )
            tc = await db.get_thermostat_config(conn, "climate.x")
            assert tc.min_setpoint == 60.8  # 16°C → 60.8°F (absolute)
            assert tc.max_setpoint == 80.6  # 27°C → 80.6°F (absolute)
            assert tc.deadband == 1.8  # 1°C delta → 1.8°F (no 32° offset)
            assert tc.overshoot_delta == 3.6  # 2°C delta → 3.6°F
        finally:
            await conn.close()

    async def test_set_thermostat_config_fahrenheit_is_identity(self):
        conn = await _conn_with_unit("F")
        try:
            server = build_server(conn)
            await server.call_tool(
                "set_thermostat_config",
                {"thermostat_entity_id": "climate.y", "min_setpoint": 60.0, "deadband": 2.0},
            )
            tc = await db.get_thermostat_config(conn, "climate.y")
            assert tc.min_setpoint == 60.0
            assert tc.deadband == 2.0
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# Issue #517: the per-schedule deadband override on the MCP write boundary.
#
# MCP is a second write boundary onto the same rows the REST API guards, so it
# must convert (DELTA, never absolute) and bound-check identically — otherwise
# an MCP client can persist a band the UI would have rejected. Because the MCP
# signature cannot distinguish "omitted" from "null", clearing the band needs
# its own `clear_deadband_override` flag.
# ---------------------------------------------------------------------------


async def _seed_schedule(
    conn: aiosqlite.Connection, room: Room, deadband_override: float | None = None
) -> Schedule:
    s = Schedule.create(
        room_id=room.id,
        days_of_week=[0, 1, 2],
        start_time=time(8, 0),
        end_time=time(17, 0),
        target_temp=70.0,
        deadband_override=deadband_override,
    )
    await db.upsert_schedule(conn, s)
    return s


async def _band(conn: aiosqlite.Connection, room_id: str) -> float | None:
    scheds = await db.get_schedules_for_room(conn, room_id)
    assert len(scheds) == 1
    return scheds[0].deadband_override


def _text(content) -> str:
    return str(content[0].text)


class TestMcpScheduleDeadbandOverride:
    # — create —

    async def test_create_schedule_stores_the_band(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await server.call_tool(
                "create_schedule",
                {
                    "room_id": room.id,
                    "days_of_week": [0],
                    "start_time": "08:00",
                    "end_time": "18:00",
                    "target_temp": 70.0,
                    "deadband_override": 2.5,
                },
            )
            assert await _band(conn, room.id) == 2.5
        finally:
            await conn.close()

    async def test_create_schedule_omitting_the_band_stores_none(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await server.call_tool(
                "create_schedule",
                {
                    "room_id": room.id,
                    "days_of_week": [0],
                    "start_time": "08:00",
                    "end_time": "18:00",
                    "target_temp": 70.0,
                },
            )
            assert await _band(conn, room.id) is None
        finally:
            await conn.close()

    async def test_create_schedule_accepts_the_inclusive_bounds(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            for band, start in ((0.0, "08:00"), (10.0, "19:00")):
                await server.call_tool(
                    "create_schedule",
                    {
                        "room_id": room.id,
                        "days_of_week": [0],
                        "start_time": start,
                        "end_time": "23:00",
                        "target_temp": 70.0,
                        "deadband_override": band,
                    },
                )
            bands = sorted(
                s.deadband_override or 0.0 for s in await db.get_schedules_for_room(conn, room.id)
            )
            assert bands == [0.0, 10.0]
        finally:
            await conn.close()

    async def test_create_schedule_out_of_range_band_errors_and_persists_nothing(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            content, _ = await server.call_tool(
                "create_schedule",
                {
                    "room_id": room.id,
                    "days_of_week": [0],
                    "start_time": "08:00",
                    "end_time": "18:00",
                    "target_temp": 70.0,
                    "deadband_override": 25.0,
                },
            )
            assert "between 0 and 10" in _text(content)
            assert await db.get_schedules_for_room(conn, room.id) == []
        finally:
            await conn.close()

    async def test_create_schedule_negative_band_errors_and_persists_nothing(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            content, _ = await server.call_tool(
                "create_schedule",
                {
                    "room_id": room.id,
                    "days_of_week": [0],
                    "start_time": "08:00",
                    "end_time": "18:00",
                    "target_temp": 70.0,
                    "deadband_override": -0.1,
                },
            )
            assert "between 0 and 10" in _text(content)
            assert await db.get_schedules_for_room(conn, room.id) == []
        finally:
            await conn.close()

    async def test_create_schedule_band_is_a_delta_in_celsius(self):
        """#284/#517: the band converts via ``delta_to_f`` (1°C → 1.8°F). An
        absolute conversion would have stored 33.8°F."""
        conn = await _conn_with_unit("C")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await server.call_tool(
                "create_schedule",
                {
                    "room_id": room.id,
                    "days_of_week": [0],
                    "start_time": "08:00",
                    "end_time": "18:00",
                    "target_temp": 21.0,
                    "deadband_override": 1.0,
                },
            )
            band = await _band(conn, room.id)
            assert band == 1.8
            assert band != 33.8  # regression to `to_f` would give this
        finally:
            await conn.close()

    async def test_create_schedule_band_bounds_apply_after_celsius_conversion(self):
        """6°C is 10.8°F — over the 10°F ceiling — so it must be rejected even
        though the raw number is well under 10."""
        conn = await _conn_with_unit("C")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            content, _ = await server.call_tool(
                "create_schedule",
                {
                    "room_id": room.id,
                    "days_of_week": [0],
                    "start_time": "08:00",
                    "end_time": "18:00",
                    "target_temp": 21.0,
                    "deadband_override": 6.0,
                },
            )
            assert "between 0 and 10" in _text(content)
            assert await db.get_schedules_for_room(conn, room.id) == []
        finally:
            await conn.close()

    # — update —

    async def test_update_schedule_sets_the_band(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            server = build_server(conn)
            await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "deadband_override": 3.0},
            )
            assert await _band(conn, room.id) == 3.0
        finally:
            await conn.close()

    async def test_update_schedule_clear_flag_removes_the_band(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room, deadband_override=3.0)
            server = build_server(conn)
            await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "clear_deadband_override": True},
            )
            assert await _band(conn, room.id) is None
        finally:
            await conn.close()

    async def test_update_schedule_omitting_both_leaves_the_band_alone(self):
        """Omitted ≠ cleared: editing an unrelated field must preserve the
        band (the MCP twin of the REST presence sentinel)."""
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room, deadband_override=3.0)
            server = build_server(conn)
            await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "target_temp": 72.0},
            )
            scheds = await db.get_schedules_for_room(conn, room.id)
            assert scheds[0].target_temp == 72.0
            assert scheds[0].deadband_override == 3.0
        finally:
            await conn.close()

    async def test_update_schedule_rejects_both_band_and_clear_flag(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room, deadband_override=3.0)
            server = build_server(conn)
            content, _ = await server.call_tool(
                "update_schedule",
                {
                    "schedule_id": s.id,
                    "room_id": room.id,
                    "deadband_override": 5.0,
                    "clear_deadband_override": True,
                },
            )
            assert "not both" in _text(content)
            # Nothing at all may have changed.
            assert await _band(conn, room.id) == 3.0
        finally:
            await conn.close()

    async def test_update_schedule_rejecting_both_does_not_apply_other_fields(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room, deadband_override=3.0)
            server = build_server(conn)
            await server.call_tool(
                "update_schedule",
                {
                    "schedule_id": s.id,
                    "room_id": room.id,
                    "target_temp": 72.0,
                    "deadband_override": 5.0,
                    "clear_deadband_override": True,
                },
            )
            scheds = await db.get_schedules_for_room(conn, room.id)
            assert scheds[0].target_temp == 70.0, "the rejected call must write nothing"
            assert scheds[0].deadband_override == 3.0
        finally:
            await conn.close()

    async def test_update_schedule_out_of_range_band_errors_and_changes_nothing(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room, deadband_override=3.0)
            server = build_server(conn)
            content, _ = await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "deadband_override": 25.0},
            )
            assert "between 0 and 10" in _text(content)
            assert await _band(conn, room.id) == 3.0
        finally:
            await conn.close()

    async def test_update_schedule_band_is_a_delta_in_celsius(self):
        conn = await _conn_with_unit("C")
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            server = build_server(conn)
            await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "deadband_override": 1.0},
            )
            band = await _band(conn, room.id)
            assert band == 1.8
            assert band != 33.8
        finally:
            await conn.close()

    async def test_update_schedule_clear_flag_works_on_a_bandless_block(self):
        """Clearing an already-absent band is a harmless no-op."""
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            server = build_server(conn)
            await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "clear_deadband_override": True},
            )
            assert await _band(conn, room.id) is None
        finally:
            await conn.close()

    async def test_update_schedule_unknown_id_reports_not_found(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            content, _ = await server.call_tool(
                "update_schedule",
                {"schedule_id": "nope", "room_id": room.id, "deadband_override": 2.0},
            )
            assert "not found" in _text(content)
        finally:
            await conn.close()

    # — list —

    async def test_list_schedules_exposes_the_band_and_lifecycle_keys(self):
        """An MCP client must be able to READ back what it can write, or it
        cannot round-trip a block."""
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            await _seed_schedule(conn, room, deadband_override=2.5)
            server = build_server(conn)
            content, _ = await server.call_tool("list_schedules", {"room_id": room.id})
            data = json.loads(_text(content))
            assert len(data) == 1
            assert data[0]["deadband_override"] == 2.5
            assert data[0]["enabled"] is True
            assert data[0]["expires_at"] is None
        finally:
            await conn.close()

    async def test_list_schedules_reports_a_missing_band_as_null(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            await _seed_schedule(conn, room)
            server = build_server(conn)
            content, _ = await server.call_tool("list_schedules", {"room_id": room.id})
            assert json.loads(_text(content))[0]["deadband_override"] is None
        finally:
            await conn.close()

    async def test_create_and_update_tool_schemas_expose_the_band(self):
        """FastMCP generates the input schema from the type hints — the band
        (and the clear flag) must actually be reachable by a client."""
        conn = await _conn_with_unit("F")
        try:
            server = build_server(conn)
            tools = {t.name: t for t in await server.list_tools()}
            assert "deadband_override" in tools["create_schedule"].inputSchema["properties"]
            update_props = tools["update_schedule"].inputSchema["properties"]
            assert "deadband_override" in update_props
            assert "clear_deadband_override" in update_props
        finally:
            await conn.close()
