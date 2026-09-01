"""Smoke tests for the MCP server (Issue #282).

The MCP integration is excluded from coverage (see ``pyproject.toml``), so
nothing else imports ``mcp_server`` or actually registers the tools. The
low-level ``mcp.server.Server`` has no ``@server.tool()`` decorator — only
``MCPServer`` does — so the previous wiring raised ``AttributeError`` the moment
the first tool module registered, killing the server before it served a
request. These tests build the real server and assert every tool registers and
is callable, so that regression can't slip back in unnoticed.
"""

from __future__ import annotations

import json
from datetime import time

import aiosqlite
from mcp.server.mcpserver import MCPServer

from backend import db, schedule_rules
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
            assert isinstance(server, MCPServer)

            tools = await server.list_tools()
            names = {t.name for t in tools}
            assert names == EXPECTED_TOOLS

            # MCPServer auto-generates an object input schema from each tool's
            # type hints — the capability the low-level Server lacked.
            for t in tools:
                assert t.input_schema.get("type") == "object"
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
            props = tools["create_room"].input_schema.get("properties", {})
            assert "name" in props
            assert "thermostat_entity_id" in props
        finally:
            await conn.close()

    async def test_list_rooms_tool_is_callable(self):
        """A read tool round-trips through MCPServer.call_tool and returns text."""
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)
        try:
            server = build_server(conn)
            result = await server.call_tool("list_rooms", {})
            assert result.content
            assert result.content[0].type == "text"
            # Empty DB → an empty JSON array of rooms.
            assert result.content[0].text.strip() == "[]"
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


def _text(result) -> str:
    """First text block of a tool result.

    mcp v2's ``MCPServer.call_tool`` returns a ``CallToolResult`` rather than
    v1's ``(content, structured)`` tuple, so unwrap the object here instead of
    at every call site.
    """
    return str(result.content[0].text)


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
            # Non-overlapping windows: MCP now enforces the same overlap rule as
            # the REST boundary (#522), so two live blocks cannot share a slot.
            for band, start, end in ((0.0, "08:00", "12:00"), (10.0, "19:00", "23:00")):
                await server.call_tool(
                    "create_schedule",
                    {
                        "room_id": room.id,
                        "days_of_week": [0],
                        "start_time": start,
                        "end_time": end,
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
            result = await server.call_tool(
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
            assert "between 0 and 10" in _text(result)
            assert await db.get_schedules_for_room(conn, room.id) == []
        finally:
            await conn.close()

    async def test_create_schedule_negative_band_errors_and_persists_nothing(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            result = await server.call_tool(
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
            assert "between 0 and 10" in _text(result)
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
            result = await server.call_tool(
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
            assert "between 0 and 10" in _text(result)
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
            result = await server.call_tool(
                "update_schedule",
                {
                    "schedule_id": s.id,
                    "room_id": room.id,
                    "deadband_override": 5.0,
                    "clear_deadband_override": True,
                },
            )
            assert "not both" in _text(result)
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
            result = await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "deadband_override": 25.0},
            )
            assert "between 0 and 10" in _text(result)
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
            result = await server.call_tool(
                "update_schedule",
                {"schedule_id": "nope", "room_id": room.id, "deadband_override": 2.0},
            )
            assert "not found" in _text(result)
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
            result = await server.call_tool("list_schedules", {"room_id": room.id})
            data = json.loads(_text(result))
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
            result = await server.call_tool("list_schedules", {"room_id": room.id})
            assert json.loads(_text(result))[0]["deadband_override"] is None
        finally:
            await conn.close()

    async def test_create_and_update_tool_schemas_expose_the_band(self):
        """MCPServer generates the input schema from the type hints — the band
        (and the clear flag) must actually be reachable by a client."""
        conn = await _conn_with_unit("F")
        try:
            server = build_server(conn)
            tools = {t.name: t for t in await server.list_tools()}
            assert "deadband_override" in tools["create_schedule"].input_schema["properties"]
            update_props = tools["update_schedule"].input_schema["properties"]
            assert "deadband_override" in update_props
            assert "clear_deadband_override" in update_props
        finally:
            await conn.close()


class TestMcpScheduleLifecycle:
    """enabled / expires_at over MCP (Issue #522).

    These were REST-only, so an MCP client could create and edit blocks but
    never park or expire one — which is exactly the guest-room workflow the
    schedule docs recommend. Adding the fields also means MCP has to enforce
    the write rules the REST boundary already did, or it becomes a way to
    persist states the UI cannot produce (the #284 failure mode).
    """

    @staticmethod
    async def _block(server, room_id, **over):
        args = {
            "room_id": room_id,
            "days_of_week": [0],
            "start_time": "22:00",
            "end_time": "23:00",
            "target_temp": 68.0,
        }
        args.update(over)
        result = await server.call_tool("create_schedule", args)
        return result.content[0].text

    async def test_creates_a_parked_block(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            text = await self._block(server, room.id, enabled=False)
            assert "parked" in text
            (s,) = await db.get_schedules_for_room(conn, room.id)
            assert s.enabled is False
        finally:
            await conn.close()

    async def test_creates_a_block_with_an_expiry_stored_local_naive(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await self._block(server, room.id, expires_at="2099-08-01T09:00")
            (s,) = await db.get_schedules_for_room(conn, room.id)
            assert s.expires_at is not None
            # Naive LOCAL, matching start_time/end_time — not UTC.
            assert s.expires_at.tzinfo is None
            assert s.expires_at.isoformat(timespec="minutes") == "2099-08-01T09:00"
        finally:
            await conn.close()

    async def test_rejects_a_block_born_already_expired(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            text = await self._block(server, room.id, expires_at="2000-01-01T00:00")
            assert "past" in text.lower()
            assert await db.get_schedules_for_room(conn, room.id) == []
        finally:
            await conn.close()

    async def test_a_parked_block_may_be_born_expired(self):
        """Only an ENABLED block is nonsense to create pre-expired; a parked one
        is inert either way, so the guard must not block it."""
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await self._block(server, room.id, expires_at="2000-01-01T00:00", enabled=False)
            assert len(await db.get_schedules_for_room(conn, room.id)) == 1
        finally:
            await conn.close()

    async def test_rejects_an_overlapping_live_block(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await self._block(server, room.id)
            text = await self._block(server, room.id, start_time="22:30")
            assert "Overlaps" in text
            assert len(await db.get_schedules_for_room(conn, room.id)) == 1
        finally:
            await conn.close()

    async def test_a_parked_block_does_not_reserve_its_slot(self):
        """The whole point of parking: two blocks can hold the same window as
        long as only one is live, which is what lets a guest block swap in."""
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await self._block(server, room.id, enabled=False)
            await self._block(server, room.id)
            assert len(await db.get_schedules_for_room(conn, room.id)) == 2
        finally:
            await conn.close()

    async def test_update_parks_and_re_arms(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await self._block(server, room.id)
            (s,) = await db.get_schedules_for_room(conn, room.id)

            await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "enabled": False},
            )
            assert (await db.get_schedules_for_room(conn, room.id))[0].enabled is False

            await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "enabled": True},
            )
            assert (await db.get_schedules_for_room(conn, room.id))[0].enabled is True
        finally:
            await conn.close()

    async def test_re_arming_into_an_occupied_slot_is_refused(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await self._block(server, room.id, enabled=False)
            await self._block(server, room.id)
            parked = next(
                s for s in await db.get_schedules_for_room(conn, room.id) if not s.enabled
            )

            result = await server.call_tool(
                "update_schedule",
                {"schedule_id": parked.id, "room_id": room.id, "enabled": True},
            )
            assert "Overlaps" in result.content[0].text
            still_parked = next(
                s for s in await db.get_schedules_for_room(conn, room.id) if s.id == parked.id
            )
            assert still_parked.enabled is False, "a refused re-arm must persist nothing"
        finally:
            await conn.close()

    async def test_update_sets_and_clears_the_expiry(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await self._block(server, room.id)
            (s,) = await db.get_schedules_for_room(conn, room.id)

            await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "expires_at": "2099-09-09T08:00"},
            )
            assert (await db.get_schedules_for_room(conn, room.id))[0].expires_at is not None

            # Omitting it must LEAVE it alone, which is why clearing needs a flag.
            await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "target_temp": 70.0},
            )
            assert (await db.get_schedules_for_room(conn, room.id))[0].expires_at is not None

            await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "clear_expires_at": True},
            )
            assert (await db.get_schedules_for_room(conn, room.id))[0].expires_at is None
        finally:
            await conn.close()

    async def test_expiry_and_clear_together_is_an_error_that_changes_nothing(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await self._block(server, room.id, expires_at="2099-09-09T08:00")
            (s,) = await db.get_schedules_for_room(conn, room.id)

            result = await server.call_tool(
                "update_schedule",
                {
                    "schedule_id": s.id,
                    "room_id": room.id,
                    "expires_at": "2099-10-10T08:00",
                    "clear_expires_at": True,
                },
            )
            assert "not both" in result.content[0].text
            after = (await db.get_schedules_for_room(conn, room.id))[0]
            assert after.expires_at.isoformat(timespec="minutes") == "2099-09-09T08:00"
        finally:
            await conn.close()

    async def test_an_aware_expiry_is_converted_to_local_naive(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await self._block(server, room.id, expires_at="2099-08-01T09:00+00:00")
            (s,) = await db.get_schedules_for_room(conn, room.id)
            assert s.expires_at is not None and s.expires_at.tzinfo is None
        finally:
            await conn.close()

    async def test_malformed_expiry_errors_and_persists_nothing(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            text = await self._block(server, room.id, expires_at="next tuesday")
            assert "ISO" in text
            assert await db.get_schedules_for_room(conn, room.id) == []
        finally:
            await conn.close()

    async def test_target_temp_is_bounded_like_the_rest_boundary(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            text = await self._block(server, room.id, target_temp=200.0)
            assert "between 40 and 90" in text
            assert await db.get_schedules_for_room(conn, room.id) == []
        finally:
            await conn.close()

    async def test_target_temp_bound_applies_after_celsius_conversion(self):
        """100 °C is 212 °F. The raw number is under 90, the converted one is
        not — the bound has to be judged on the stored value."""
        conn = await _conn_with_unit("C")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            text = await self._block(server, room.id, target_temp=100.0)
            assert "between 40 and 90" in text
            assert await db.get_schedules_for_room(conn, room.id) == []
        finally:
            await conn.close()


class TestMcpScheduleName:
    """The optional display name over MCP (Issue #520).

    Same reasoning as #517's band: MCP is a second write boundary onto the same
    rows, so it must normalize and bound the name through the shared
    `schedule_rules.normalize_name` — otherwise an MCP client can persist a
    name the UI would have rejected (the #284 failure mode). And because the
    MCP signature cannot distinguish "omitted" from "null", un-naming a block
    needs its own `clear_name` flag.
    """

    @staticmethod
    async def _block(server, room_id, **over):
        args = {
            "room_id": room_id,
            "days_of_week": [0],
            "start_time": "22:00",
            "end_time": "23:00",
            "target_temp": 68.0,
        }
        args.update(over)
        result = await server.call_tool("create_schedule", args)
        return result.content[0].text

    # — create —

    async def test_create_stores_the_name(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            text = await self._block(server, room.id, name="Weekday night setback")
            (s,) = await db.get_schedules_for_room(conn, room.id)
            assert s.name == "Weekday night setback"
            # The id is echoed too — it is what a follow-up update/delete needs.
            assert s.id in text
            assert "Weekday night setback" in text
        finally:
            await conn.close()

    async def test_create_without_a_name_stores_none(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await self._block(server, room.id)
            (s,) = await db.get_schedules_for_room(conn, room.id)
            assert s.name is None
            assert s.display_name == s.id
        finally:
            await conn.close()

    async def test_create_normalizes_whitespace(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await self._block(server, room.id, name="  Night   setback \n")
            (s,) = await db.get_schedules_for_room(conn, room.id)
            assert s.name == "Night setback"
        finally:
            await conn.close()

    async def test_create_treats_a_blank_name_as_unnamed(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await self._block(server, room.id, name="   ")
            (s,) = await db.get_schedules_for_room(conn, room.id)
            assert s.name is None
        finally:
            await conn.close()

    async def test_create_rejects_an_over_long_name_and_stores_nothing(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            text = await self._block(
                server, room.id, name="x" * (schedule_rules.MAX_NAME_LENGTH + 1)
            )
            assert "name" in text
            assert await db.get_schedules_for_room(conn, room.id) == []
        finally:
            await conn.close()

    # — update —

    async def test_update_renames_a_block_without_moving_its_id(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            server = build_server(conn)
            await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "name": " Guest stay "},
            )
            (stored,) = await db.get_schedules_for_room(conn, room.id)
            assert stored.name == "Guest stay"
            assert stored.id == s.id
        finally:
            await conn.close()

    async def test_update_clear_name_returns_the_block_to_unnamed(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            s.name = "Guest stay"
            await db.upsert_schedule(conn, s)
            server = build_server(conn)
            await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "clear_name": True},
            )
            (stored,) = await db.get_schedules_for_room(conn, room.id)
            assert stored.name is None
        finally:
            await conn.close()

    async def test_update_omitting_the_name_preserves_it(self):
        """`None` means "not supplied" in this signature — editing another
        field must not silently un-name the block."""
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            s.name = "Guest stay"
            await db.upsert_schedule(conn, s)
            server = build_server(conn)
            await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "target_temp": 71.0},
            )
            (stored,) = await db.get_schedules_for_room(conn, room.id)
            assert stored.name == "Guest stay"
            assert stored.target_temp == 71.0
        finally:
            await conn.close()

    async def test_update_rejects_name_and_clear_name_together(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            s.name = "Guest stay"
            await db.upsert_schedule(conn, s)
            server = build_server(conn)
            result = await server.call_tool(
                "update_schedule",
                {
                    "schedule_id": s.id,
                    "room_id": room.id,
                    "name": "Renamed",
                    "clear_name": True,
                },
            )
            assert "not both" in _text(result)
            (stored,) = await db.get_schedules_for_room(conn, room.id)
            assert stored.name == "Guest stay"
        finally:
            await conn.close()

    async def test_update_rejects_an_over_long_name_and_keeps_the_stored_one(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            s.name = "Guest stay"
            await db.upsert_schedule(conn, s)
            server = build_server(conn)
            result = await server.call_tool(
                "update_schedule",
                {
                    "schedule_id": s.id,
                    "room_id": room.id,
                    "name": "x" * (schedule_rules.MAX_NAME_LENGTH + 1),
                },
            )
            assert "name" in _text(result)
            (stored,) = await db.get_schedules_for_room(conn, room.id)
            assert stored.name == "Guest stay"
        finally:
            await conn.close()

    # — list —

    async def test_list_exposes_the_name(self):
        """An MCP client must be able to READ back what it can write — and #519
        needs the name/id pair to build its HA entity names."""
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            s.name = "Night setback"
            await db.upsert_schedule(conn, s)
            server = build_server(conn)
            result = await server.call_tool("list_schedules", {"room_id": room.id})
            data = json.loads(_text(result))
            assert data[0]["name"] == "Night setback"
            assert data[0]["id"] == s.id
        finally:
            await conn.close()

    async def test_list_reports_a_missing_name_as_null(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            await _seed_schedule(conn, room)
            server = build_server(conn)
            result = await server.call_tool("list_schedules", {"room_id": room.id})
            assert json.loads(_text(result))[0]["name"] is None
        finally:
            await conn.close()

    async def test_create_and_update_tool_schemas_expose_the_name(self):
        conn = await _conn_with_unit("F")
        try:
            server = build_server(conn)
            tools = {t.name: t for t in await server.list_tools()}
            assert "name" in tools["create_schedule"].input_schema["properties"]
            update_props = tools["update_schedule"].input_schema["properties"]
            assert "name" in update_props
            assert "clear_name" in update_props
        finally:
            await conn.close()

    async def test_tool_docs_quote_the_real_length_bound(self):
        """The docstrings state the limit in characters so an MCP client can
        respect it without a failed round trip — keep that number honest."""
        conn = await _conn_with_unit("F")
        try:
            server = build_server(conn)
            tools = {t.name: t for t in await server.list_tools()}
            limit = str(schedule_rules.MAX_NAME_LENGTH)
            assert limit in (tools["create_schedule"].description or "")
            assert limit in (tools["update_schedule"].description or "")
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# Issue #576: the temporary-hold tool mirrors the REST hold boundary — the
# (0, 8] duration cap and the Eco opt-IN flag must behave identically, or an
# MCP client can create a hold the UI would have refused.
# ---------------------------------------------------------------------------


class TestMcpRoomHold:
    async def test_set_room_override_persists_respect_eco(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await server.call_tool(
                "set_room_override",
                {"room_id": room.id, "target_temp": 72.0, "respect_eco": True},
            )
            ov = await db.get_room_override(conn, room.id)
            assert ov is not None
            assert ov.respect_eco is True
        finally:
            await conn.close()

    async def test_set_room_override_defaults_to_ignoring_eco(self):
        """#419: a hold is never Eco-relaxed unless the caller opts in."""
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await server.call_tool("set_room_override", {"room_id": room.id, "target_temp": 72.0})
            ov = await db.get_room_override(conn, room.id)
            assert ov is not None
            assert ov.respect_eco is False
        finally:
            await conn.close()

    async def test_set_room_override_accepts_the_8h_ceiling(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            result = await server.call_tool(
                "set_room_override",
                {"room_id": room.id, "target_temp": 72.0, "duration_hours": 8.0},
            )
            assert "Override set" in _text(result)
            assert await db.get_room_override(conn, room.id) is not None
        finally:
            await conn.close()

    async def test_set_room_override_rejects_9h_and_persists_nothing(self):
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            result = await server.call_tool(
                "set_room_override",
                {"room_id": room.id, "target_temp": 72.0, "duration_hours": 9.0},
            )
            assert "duration_hours must be greater than 0 and at most 8" in _text(result)
            assert await db.get_room_override(conn, room.id) is None
        finally:
            await conn.close()

    async def test_set_room_override_rejects_out_of_bounds_target_and_persists_nothing(self):
        """#576 closed a gap: this tool accepted any target while the REST
        write boundary enforced 40-90°F post-conversion. Both sides now agree."""
        conn = await _conn_with_unit("F")
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            for bad_target in (150.0, 30.0):
                result = await server.call_tool(
                    "set_room_override",
                    {"room_id": room.id, "target_temp": bad_target},
                )
                assert "target_temp must be between 40 and 90°F" in _text(result)
                assert await db.get_room_override(conn, room.id) is None
        finally:
            await conn.close()
