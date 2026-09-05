"""Behavioural tests for ``backend/mcp_tools/rooms.py``.

``test_mcp_server.py`` covers the temperature-conversion write boundary and the
hold-duration/target guards. This file exercises the rest of the room tool
surface — the read tools' body (list/get), the per-field ``update_room``
branches, the sensor/vent/presence membership tools and
``clear_room_override`` — always asserting the state actually written to (or
read back from) the DB, not merely that the tool returned a string.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta

import aiosqlite

from backend import db
from backend.mcp_server import build_server
from backend.models import (
    Room,
    RoomOverride,
    RoomPresenceSensor,
    RoomSensor,
    RoomVent,
    Schedule,
)


def _text(result) -> str:
    """First text block of a ``CallToolResult`` (mcp SDK v2)."""
    return str(result.content[0].text)


async def _conn(unit: str = "F") -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    await db.set_system_setting(conn, "temperature_unit", unit)
    return conn


async def _seed_room(conn: aiosqlite.Connection, **kwargs) -> Room:
    room = Room.create(
        name=kwargs.pop("name", "Bedroom"),
        thermostat_entity_id=kwargs.pop("thermostat_entity_id", "climate.main"),
        **kwargs,
    )
    await db.upsert_room(conn, room)
    return room


# ---------------------------------------------------------------------------
# list_rooms — the per-room fan-out body (sensor/vent/presence counts)
# ---------------------------------------------------------------------------


class TestListRooms:
    async def test_counts_sensors_vents_and_presence_per_room(self):
        conn = await _conn()
        try:
            a = await _seed_room(conn, name="Upstairs Office")
            b = await _seed_room(conn, name="Guest Room")
            await db.add_room_sensor(conn, RoomSensor.create(a.id, "sensor.office_a"))
            await db.add_room_sensor(conn, RoomSensor.create(a.id, "sensor.office_b"))
            await db.add_room_vent(conn, RoomVent.create(a.id, "cover.office_vent"))
            await db.add_room_presence_sensor(
                conn, RoomPresenceSensor.create(a.id, "binary_sensor.office_occupancy")
            )

            server = build_server(conn)
            payload = json.loads(_text(await server.call_tool("list_rooms", {})))

            by_id = {r["id"]: r for r in payload}
            assert set(by_id) == {a.id, b.id}
            assert by_id[a.id]["sensor_count"] == 2
            assert by_id[a.id]["vent_count"] == 1
            assert by_id[a.id]["presence_sensor_count"] == 1
            # A room with no members reports zeroes, not a missing key.
            assert by_id[b.id]["sensor_count"] == 0
            assert by_id[b.id]["vent_count"] == 0
            assert by_id[b.id]["presence_sensor_count"] == 0
            # The room's own fields are spread in alongside the counts.
            assert by_id[a.id]["name"] == "Upstairs Office"
            assert by_id[a.id]["thermostat_entity_id"] == "climate.main"
        finally:
            await conn.close()

    async def test_counts_are_scoped_to_their_own_room(self):
        """A sensor on room A must not inflate room B's count."""
        conn = await _conn()
        try:
            a = await _seed_room(conn, name="A")
            b = await _seed_room(conn, name="B")
            await db.add_room_sensor(conn, RoomSensor.create(a.id, "sensor.a"))
            await db.add_room_vent(conn, RoomVent.create(b.id, "cover.b"))

            server = build_server(conn)
            by_id = {
                r["id"]: r for r in json.loads(_text(await server.call_tool("list_rooms", {})))
            }
            assert (by_id[a.id]["sensor_count"], by_id[a.id]["vent_count"]) == (1, 0)
            assert (by_id[b.id]["sensor_count"], by_id[b.id]["vent_count"]) == (0, 1)
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# get_room — detail read, including the not-found path
# ---------------------------------------------------------------------------


class TestGetRoom:
    async def test_unknown_id_reports_not_found(self):
        conn = await _conn()
        try:
            server = build_server(conn)
            result = await server.call_tool("get_room", {"room_id": "no-such-room"})
            assert _text(result) == "Room no-such-room not found"
        finally:
            await conn.close()

    async def test_returns_members_and_schedules(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn, name="Upstairs Office", notes="corner room")
            await db.add_room_sensor(conn, RoomSensor.create(room.id, "sensor.office_temp"))
            await db.add_room_vent(conn, RoomVent.create(room.id, "cover.office_vent"))
            await db.add_room_presence_sensor(
                conn, RoomPresenceSensor.create(room.id, "binary_sensor.office_occupancy")
            )
            sched = Schedule.create(
                room_id=room.id,
                days_of_week=[0, 1, 2, 3, 4],
                start_time=time(8, 0),
                end_time=time(17, 0),
                target_temp=70.0,
                name="Workday",
            )
            await db.upsert_schedule(conn, sched)

            server = build_server(conn)
            data = json.loads(_text(await server.call_tool("get_room", {"room_id": room.id})))

            assert data["id"] == room.id
            assert data["name"] == "Upstairs Office"
            assert data["notes"] == "corner room"
            assert [s["entity_id"] for s in data["sensors"]] == ["sensor.office_temp"]
            assert [v["entity_id"] for v in data["vents"]] == ["cover.office_vent"]
            assert [p["entity_id"] for p in data["presence_sensors"]] == [
                "binary_sensor.office_occupancy"
            ]
            assert data["schedules"] == [
                {
                    "id": sched.id,
                    "name": "Workday",
                    "days_of_week": [0, 1, 2, 3, 4],
                    "start_time": "08:00:00",
                    "end_time": "17:00:00",
                    # Stored and reported in °F (the storage unit), per the
                    # single-write-boundary contract.
                    "target_temp": 70.0,
                }
            ]
        finally:
            await conn.close()

    async def test_a_bare_room_returns_empty_member_lists(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            data = json.loads(_text(await server.call_tool("get_room", {"room_id": room.id})))
            assert data["sensors"] == []
            assert data["vents"] == []
            assert data["presence_sensors"] == []
            assert data["schedules"] == []
        finally:
            await conn.close()

    async def test_only_this_rooms_members_are_returned(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn, name="A")
            other = await _seed_room(conn, name="B")
            await db.add_room_sensor(conn, RoomSensor.create(other.id, "sensor.b"))
            await db.upsert_schedule(
                conn,
                Schedule.create(
                    room_id=other.id,
                    days_of_week=[6],
                    start_time=time(1, 0),
                    end_time=time(2, 0),
                    target_temp=68.0,
                ),
            )
            server = build_server(conn)
            data = json.loads(_text(await server.call_tool("get_room", {"room_id": room.id})))
            assert data["sensors"] == []
            assert data["schedules"] == []
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# update_room — one test per field branch, each asserting the stored row
# ---------------------------------------------------------------------------


class TestUpdateRoom:
    async def test_unknown_id_reports_not_found_and_creates_nothing(self):
        conn = await _conn()
        try:
            server = build_server(conn)
            result = await server.call_tool("update_room", {"room_id": "ghost", "name": "Nowhere"})
            assert _text(result) == "Room ghost not found"
            assert await db.get_all_rooms(conn) == []
        finally:
            await conn.close()

    async def test_sets_name_only(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn, name="Old", notes="keep me")
            server = build_server(conn)
            result = await server.call_tool("update_room", {"room_id": room.id, "name": "New"})
            assert _text(result) == f"Updated room {room.id}"
            stored = await db.get_room(conn, room.id)
            assert stored.name == "New"
            # Omitted fields are untouched.
            assert stored.thermostat_entity_id == "climate.main"
            assert stored.notes == "keep me"
            assert stored.presence_holdover_hours == 2.0
            assert stored.include_thermostat_sensor is False
            assert stored.system_wide_temp is None
        finally:
            await conn.close()

    async def test_sets_thermostat_entity_id_only(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn, name="Bedroom")
            server = build_server(conn)
            await server.call_tool(
                "update_room",
                {"room_id": room.id, "thermostat_entity_id": "climate.upstairs"},
            )
            stored = await db.get_room(conn, room.id)
            assert stored.thermostat_entity_id == "climate.upstairs"
            assert stored.name == "Bedroom"
        finally:
            await conn.close()

    async def test_sets_presence_holdover_hours_only(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await server.call_tool(
                "update_room", {"room_id": room.id, "presence_holdover_hours": 4.5}
            )
            stored = await db.get_room(conn, room.id)
            assert stored.presence_holdover_hours == 4.5
            assert stored.name == "Bedroom"
            assert stored.system_wide_temp is None
        finally:
            await conn.close()

    async def test_sets_include_thermostat_sensor_true_and_back_to_false(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await server.call_tool(
                "update_room", {"room_id": room.id, "include_thermostat_sensor": True}
            )
            assert (await db.get_room(conn, room.id)).include_thermostat_sensor is True

            # False is a real value, not "omitted" — it must be applied.
            await server.call_tool(
                "update_room", {"room_id": room.id, "include_thermostat_sensor": False}
            )
            assert (await db.get_room(conn, room.id)).include_thermostat_sensor is False
        finally:
            await conn.close()

    async def test_sets_notes_only(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn, name="Bedroom")
            server = build_server(conn)
            await server.call_tool(
                "update_room", {"room_id": room.id, "notes": "south-facing, gets sun"}
            )
            stored = await db.get_room(conn, room.id)
            assert stored.notes == "south-facing, gets sun"
            assert stored.name == "Bedroom"
        finally:
            await conn.close()

    async def test_clearing_notes_to_empty_string_is_applied(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn, notes="stale note")
            server = build_server(conn)
            await server.call_tool("update_room", {"room_id": room.id, "notes": ""})
            assert (await db.get_room(conn, room.id)).notes == ""
        finally:
            await conn.close()

    async def test_updates_every_field_at_once(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await server.call_tool(
                "update_room",
                {
                    "room_id": room.id,
                    "name": "Den",
                    "thermostat_entity_id": "climate.den",
                    "system_wide_temp": 72.0,
                    "presence_holdover_hours": 1.0,
                    "include_thermostat_sensor": True,
                    "notes": "n",
                },
            )
            stored = await db.get_room(conn, room.id)
            assert stored.name == "Den"
            assert stored.thermostat_entity_id == "climate.den"
            assert stored.system_wide_temp == 72.0  # °F in, °F stored
            assert stored.presence_holdover_hours == 1.0
            assert stored.include_thermostat_sensor is True
            assert stored.notes == "n"
        finally:
            await conn.close()

    async def test_omitting_system_wide_temp_preserves_the_stored_value(self):
        """Only the named field moves; the fixed target survives a rename."""
        conn = await _conn("C")
        try:
            room = await _seed_room(conn, system_wide_temp=69.8)
            server = build_server(conn)
            await server.call_tool("update_room", {"room_id": room.id, "name": "Renamed"})
            stored = await db.get_room(conn, room.id)
            assert stored.name == "Renamed"
            assert stored.system_wide_temp == 69.8
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# delete_room
# ---------------------------------------------------------------------------


class TestDeleteRoom:
    async def test_removes_the_room(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            keep = await _seed_room(conn, name="Keeper")
            server = build_server(conn)
            result = await server.call_tool("delete_room", {"room_id": room.id})
            assert _text(result) == f"Deleted room {room.id}"
            assert await db.get_room(conn, room.id) is None
            assert (await db.get_room(conn, keep.id)) is not None
        finally:
            await conn.close()

    async def test_deleting_an_unknown_room_is_a_no_op(self):
        conn = await _conn()
        try:
            keep = await _seed_room(conn)
            server = build_server(conn)
            result = await server.call_tool("delete_room", {"room_id": "ghost"})
            assert _text(result) == "Deleted room ghost"
            assert (await db.get_room(conn, keep.id)) is not None
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# Membership tools: sensors, vents, presence sensors
# ---------------------------------------------------------------------------


class TestSensorMembership:
    async def test_add_sensor_persists_the_row(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            result = await server.call_tool(
                "add_sensor", {"room_id": room.id, "entity_id": "sensor.office_temp"}
            )
            assert _text(result) == f"Added sensor sensor.office_temp to room {room.id}"
            sensors = await db.get_room_sensors(conn, room.id)
            assert [s.entity_id for s in sensors] == ["sensor.office_temp"]
            assert sensors[0].room_id == room.id
            assert sensors[0].id  # an id was minted
        finally:
            await conn.close()

    async def test_remove_sensor_deletes_only_the_named_entity(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await server.call_tool("add_sensor", {"room_id": room.id, "entity_id": "sensor.a"})
            await server.call_tool("add_sensor", {"room_id": room.id, "entity_id": "sensor.b"})

            result = await server.call_tool(
                "remove_sensor", {"room_id": room.id, "entity_id": "sensor.a"}
            )
            assert _text(result) == "Removed sensor sensor.a"
            assert [s.entity_id for s in await db.get_room_sensors(conn, room.id)] == ["sensor.b"]
        finally:
            await conn.close()

    async def test_remove_sensor_leaves_the_same_entity_on_another_room(self):
        conn = await _conn()
        try:
            a = await _seed_room(conn, name="A")
            b = await _seed_room(conn, name="B")
            server = build_server(conn)
            await server.call_tool("add_sensor", {"room_id": a.id, "entity_id": "sensor.shared"})
            await server.call_tool("add_sensor", {"room_id": b.id, "entity_id": "sensor.shared"})

            await server.call_tool("remove_sensor", {"room_id": a.id, "entity_id": "sensor.shared"})
            assert await db.get_room_sensors(conn, a.id) == []
            assert [s.entity_id for s in await db.get_room_sensors(conn, b.id)] == ["sensor.shared"]
        finally:
            await conn.close()


class TestVentMembership:
    async def test_add_vent_persists_the_row_with_the_default_control_method(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            result = await server.call_tool(
                "add_vent", {"room_id": room.id, "entity_id": "cover.office_vent"}
            )
            assert _text(result) == f"Added vent cover.office_vent to room {room.id}"
            vents = await db.get_room_vents(conn, room.id)
            assert [v.entity_id for v in vents] == ["cover.office_vent"]
            assert vents[0].control_method == "open_close"
        finally:
            await conn.close()

    async def test_remove_vent_deletes_only_the_named_entity(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await server.call_tool("add_vent", {"room_id": room.id, "entity_id": "cover.a"})
            await server.call_tool("add_vent", {"room_id": room.id, "entity_id": "cover.b"})

            result = await server.call_tool(
                "remove_vent", {"room_id": room.id, "entity_id": "cover.a"}
            )
            assert _text(result) == "Removed vent cover.a"
            assert [v.entity_id for v in await db.get_room_vents(conn, room.id)] == ["cover.b"]
        finally:
            await conn.close()


class TestPresenceSensorMembership:
    async def test_add_presence_sensor_persists_the_row(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            result = await server.call_tool(
                "add_presence_sensor",
                {"room_id": room.id, "entity_id": "binary_sensor.office_occupancy"},
            )
            assert _text(result) == (
                f"Added presence sensor binary_sensor.office_occupancy to room {room.id}"
            )
            presence = await db.get_room_presence_sensors(conn, room.id)
            assert [p.entity_id for p in presence] == ["binary_sensor.office_occupancy"]
            assert presence[0].room_id == room.id
            # A presence sensor is its own list — it must not land in sensors.
            assert await db.get_room_sensors(conn, room.id) == []
        finally:
            await conn.close()

    async def test_remove_presence_sensor_deletes_only_the_named_entity(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            await server.call_tool(
                "add_presence_sensor", {"room_id": room.id, "entity_id": "binary_sensor.a"}
            )
            await server.call_tool(
                "add_presence_sensor", {"room_id": room.id, "entity_id": "binary_sensor.b"}
            )

            result = await server.call_tool(
                "remove_presence_sensor", {"room_id": room.id, "entity_id": "binary_sensor.a"}
            )
            assert _text(result) == "Removed presence sensor binary_sensor.a"
            assert [p.entity_id for p in await db.get_room_presence_sensors(conn, room.id)] == [
                "binary_sensor.b"
            ]
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# clear_room_override
# ---------------------------------------------------------------------------


class TestClearRoomOverride:
    async def test_clears_an_existing_hold(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            await db.set_room_override(
                conn,
                RoomOverride(
                    room_id=room.id,
                    target_temp=72.0,
                    expires_at=datetime.now(UTC) + timedelta(hours=2),
                ),
            )
            assert await db.get_room_override(conn, room.id) is not None

            server = build_server(conn)
            result = await server.call_tool("clear_room_override", {"room_id": room.id})
            assert _text(result) == f"Override cleared for room {room.id}"
            assert await db.get_room_override(conn, room.id) is None
        finally:
            await conn.close()

    async def test_clearing_one_room_leaves_another_rooms_hold_alone(self):
        conn = await _conn()
        try:
            a = await _seed_room(conn, name="A")
            b = await _seed_room(conn, name="B")
            expires = datetime.now(UTC) + timedelta(hours=2)
            for room_id in (a.id, b.id):
                await db.set_room_override(
                    conn,
                    RoomOverride(room_id=room_id, target_temp=72.0, expires_at=expires),
                )

            server = build_server(conn)
            await server.call_tool("clear_room_override", {"room_id": a.id})
            assert await db.get_room_override(conn, a.id) is None
            assert await db.get_room_override(conn, b.id) is not None
        finally:
            await conn.close()

    async def test_clearing_when_there_is_no_hold_is_a_no_op(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            server = build_server(conn)
            result = await server.call_tool("clear_room_override", {"room_id": room.id})
            assert _text(result) == f"Override cleared for room {room.id}"
            assert await db.get_room_override(conn, room.id) is None
        finally:
            await conn.close()
