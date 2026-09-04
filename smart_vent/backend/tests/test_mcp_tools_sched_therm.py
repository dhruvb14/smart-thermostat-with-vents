"""Coverage for the MCP schedule/thermostat tools and the stdio entry point.

``backend/mcp_server.py``'s ``main()`` is the stdio transport entry point — the
process HA's add-on never runs but a Claude Code ``mcpServers`` block does. Its
one load-bearing property is the ``try/finally``: the dedicated DB connection
must be closed even when the transport dies, or a crashed MCP process leaves the
SQLite file locked behind a stale handle.

The schedule/thermostat gaps here are the mutation and rejection arms of
``update_schedule`` (window edits, out-of-range targets, unparsable and stale
expiries), ``delete_schedule``, and the two thermostat paths (listing configs,
setting ``cycle_timeout_hours``). Per the repo's unit contract every temperature
crossing this boundary is interpreted in the active display unit and stored as
°F, so the Celsius assertions below check the stored °F, never the input echo.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta

import aiosqlite
import pytest

from backend import db, mcp_server, tz
from backend.mcp_server import build_server
from backend.models import Room, Schedule, ThermostatConfig

# Deliberately absolute instants rather than offsets from ``now``: the expiry
# guard compares against the wall clock, and a fixed year keeps the verdict the
# same on every run without pinning the clock.
PAST_EXPIRY = "2020-01-01T00:00"
FUTURE_EXPIRY = "2099-01-01T00:00"


def _text(result) -> str:
    """First text block of an ``MCPServer.call_tool`` result."""
    return str(result.content[0].text)


async def _conn(unit: str = "F") -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    await db.set_system_setting(conn, "temperature_unit", unit)
    return conn


async def _seed_room(conn: aiosqlite.Connection) -> Room:
    room = Room.create(name="Bedroom", thermostat_entity_id="climate.x")
    await db.upsert_room(conn, room)
    return room


async def _seed_schedule(
    conn: aiosqlite.Connection,
    room: Room,
    *,
    enabled: bool = True,
    expires_at: datetime | None = None,
) -> Schedule:
    s = Schedule.create(
        room_id=room.id,
        days_of_week=[0, 1, 2],
        start_time=time(8, 0),
        end_time=time(17, 0),
        target_temp=70.0,
        enabled=enabled,
        expires_at=expires_at,
    )
    await db.upsert_schedule(conn, s)
    return s


async def _only(conn: aiosqlite.Connection, room_id: str) -> Schedule:
    scheds = await db.get_schedules_for_room(conn, room_id)
    assert len(scheds) == 1
    return scheds[0]


# ---------------------------------------------------------------------------
# update_schedule — the mutation arms
# ---------------------------------------------------------------------------


class TestUpdateScheduleWindow:
    async def test_updates_days_and_both_times(self):
        """days_of_week / start_time / end_time each write through to the row."""
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            server = build_server(conn)
            result = await server.call_tool(
                "update_schedule",
                {
                    "schedule_id": s.id,
                    "room_id": room.id,
                    "days_of_week": [3, 4],
                    "start_time": "06:30",
                    "end_time": "09:45",
                },
            )
            assert f"Updated schedule {s.id}" in _text(result)
            stored = await _only(conn, room.id)
            assert stored.days_of_week == [3, 4]
            assert stored.start_time == time(6, 30)
            assert stored.end_time == time(9, 45)
        finally:
            await conn.close()

    async def test_updating_only_the_start_time_leaves_the_rest_alone(self):
        """Each field is independently optional — no field defaults leak in."""
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            server = build_server(conn)
            await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "start_time": "07:15"},
            )
            stored = await _only(conn, room.id)
            assert stored.start_time == time(7, 15)
            assert stored.end_time == time(17, 0)
            assert stored.days_of_week == [0, 1, 2]
            assert stored.target_temp == 70.0
        finally:
            await conn.close()

    async def test_window_edit_is_refused_when_it_would_overlap_a_live_block(self):
        """The conflict guard runs on the block's FINAL shape, post-mutation."""
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            other = Schedule.create(
                room_id=room.id,
                days_of_week=[0],
                start_time=time(20, 0),
                end_time=time(22, 0),
                target_temp=66.0,
            )
            await db.upsert_schedule(conn, other)
            server = build_server(conn)
            result = await server.call_tool(
                "update_schedule",
                {
                    "schedule_id": s.id,
                    "room_id": room.id,
                    "start_time": "19:00",
                    "end_time": "21:00",
                },
            )
            assert "Overlaps with existing block" in _text(result)
            scheds = {x.id: x for x in await db.get_schedules_for_room(conn, room.id)}
            assert scheds[s.id].start_time == time(8, 0)
            assert scheds[s.id].end_time == time(17, 0)
        finally:
            await conn.close()


class TestUpdateScheduleTargetTemp:
    async def test_out_of_range_target_is_refused_and_persists_nothing(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            server = build_server(conn)
            result = await server.call_tool(
                "update_schedule",
                {
                    "schedule_id": s.id,
                    "room_id": room.id,
                    "days_of_week": [5],
                    "target_temp": 200.0,
                },
            )
            assert "between 40 and 90" in _text(result)
            stored = await _only(conn, room.id)
            assert stored.target_temp == 70.0
            # The early return happens before the upsert, so the days_of_week
            # edit in the same call must not land either.
            assert stored.days_of_week == [0, 1, 2]
        finally:
            await conn.close()

    async def test_celsius_target_is_bounded_after_conversion_then_stored_as_f(self):
        """21°C → 69.8°F stored; the 40–90°F envelope is applied to the °F value."""
        conn = await _conn("C")
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            server = build_server(conn)
            await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "target_temp": 21.0},
            )
            assert (await _only(conn, room.id)).target_temp == 69.8

            # 60°C ≈ 140°F — inside a naive "40–90" check on the raw input, but
            # out of range once converted. The bound must apply post-conversion.
            result = await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "target_temp": 60.0},
            )
            assert "between 40 and 90" in _text(result)
            assert (await _only(conn, room.id)).target_temp == 69.8
        finally:
            await conn.close()


class TestUpdateScheduleExpiry:
    async def test_unparsable_expires_at_is_refused_and_persists_nothing(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            server = build_server(conn)
            result = await server.call_tool(
                "update_schedule",
                {
                    "schedule_id": s.id,
                    "room_id": room.id,
                    "expires_at": "next tuesday-ish",
                    "days_of_week": [6],
                },
            )
            text = _text(result)
            assert "local ISO timestamp" in text
            assert "clear_expires_at" in text
            stored = await _only(conn, room.id)
            assert stored.expires_at is None
            assert stored.days_of_week == [0, 1, 2]
        finally:
            await conn.close()

    async def test_future_expires_at_is_accepted(self):
        """The happy path the rejection arms are contrasted against."""
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            server = build_server(conn)
            result = await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "expires_at": FUTURE_EXPIRY},
            )
            assert f"Updated schedule {s.id}" in _text(result)
            assert (await _only(conn, room.id)).expires_at == datetime(2099, 1, 1, 0, 0)
        finally:
            await conn.close()

    async def test_past_expires_at_is_refused(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            server = build_server(conn)
            result = await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "expires_at": PAST_EXPIRY},
            )
            assert "expires_at is in the past" in _text(result)
            assert (await _only(conn, room.id)).expires_at is None
        finally:
            await conn.close()

    async def test_rearming_a_block_with_a_stale_expiry_is_refused(self):
        """enabled=True alone re-runs the expiry guard on the stored expiry."""
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(
                conn,
                room,
                enabled=False,
                expires_at=datetime(2020, 1, 1, 0, 0),
            )
            server = build_server(conn)
            result = await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "enabled": True},
            )
            assert "expires_at is in the past" in _text(result)
            assert (await _only(conn, room.id)).enabled is False
        finally:
            await conn.close()

    async def test_rearming_with_a_live_expiry_is_allowed(self):
        """The guard must key on the expiry being past, not merely on enabled=True."""
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            future = tz.now_local().replace(tzinfo=None) + timedelta(days=365)
            s = await _seed_schedule(conn, room, enabled=False, expires_at=future)
            server = build_server(conn)
            result = await server.call_tool(
                "update_schedule",
                {"schedule_id": s.id, "room_id": room.id, "enabled": True},
            )
            assert f"Updated schedule {s.id}" in _text(result)
            assert (await _only(conn, room.id)).enabled is True
        finally:
            await conn.close()

    async def test_clearing_a_stale_expiry_re_arms_the_block(self):
        """clear_expires_at also arms the guard — but on a now-null expiry it passes."""
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(
                conn,
                room,
                enabled=False,
                expires_at=datetime(2020, 1, 1, 0, 0),
            )
            server = build_server(conn)
            result = await server.call_tool(
                "update_schedule",
                {
                    "schedule_id": s.id,
                    "room_id": room.id,
                    "enabled": True,
                    "clear_expires_at": True,
                },
            )
            assert f"Updated schedule {s.id}" in _text(result)
            stored = await _only(conn, room.id)
            assert stored.expires_at is None
            assert stored.enabled is True
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# delete_schedule
# ---------------------------------------------------------------------------


class TestDeleteSchedule:
    async def test_deletes_the_named_block_only(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            doomed = await _seed_schedule(conn, room)
            keeper = Schedule.create(
                room_id=room.id,
                days_of_week=[5, 6],
                start_time=time(22, 0),
                end_time=time(23, 0),
                target_temp=64.0,
            )
            await db.upsert_schedule(conn, keeper)
            server = build_server(conn)
            result = await server.call_tool("delete_schedule", {"schedule_id": doomed.id})
            assert f"Deleted schedule {doomed.id}" in _text(result)
            remaining = await db.get_schedules_for_room(conn, room.id)
            assert [x.id for x in remaining] == [keeper.id]
        finally:
            await conn.close()

    async def test_deleting_an_unknown_id_is_a_no_op(self):
        conn = await _conn()
        try:
            room = await _seed_room(conn)
            s = await _seed_schedule(conn, room)
            server = build_server(conn)
            result = await server.call_tool("delete_schedule", {"schedule_id": "sched-does-not-exist"})
            assert "Deleted schedule sched-does-not-exist" in _text(result)
            assert [x.id for x in await db.get_schedules_for_room(conn, room.id)] == [s.id]
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# thermostats.py
# ---------------------------------------------------------------------------


class TestListThermostatConfigs:
    async def test_empty_db_returns_an_empty_array(self):
        conn = await _conn()
        try:
            server = build_server(conn)
            result = await server.call_tool("list_thermostat_configs", {})
            assert json.loads(_text(result)) == []
        finally:
            await conn.close()

    async def test_lists_every_stored_config_as_json(self):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, ThermostatConfig(thermostat_entity_id="climate.up", min_setpoint=62.0)
            )
            await db.upsert_thermostat_config(
                conn, ThermostatConfig(thermostat_entity_id="climate.down", deadband=1.5)
            )
            server = build_server(conn)
            payload = json.loads(_text(await server.call_tool("list_thermostat_configs", {})))
            by_id = {c["thermostat_entity_id"]: c for c in payload}
            assert set(by_id) == {"climate.up", "climate.down"}
            # Stored °F is emitted raw — the listing does not convert.
            assert by_id["climate.up"]["min_setpoint"] == 62.0
            assert by_id["climate.down"]["deadband"] == 1.5
        finally:
            await conn.close()

    async def test_listing_reflects_a_write_made_through_the_set_tool(self):
        conn = await _conn()
        try:
            server = build_server(conn)
            await server.call_tool(
                "set_thermostat_config",
                {"thermostat_entity_id": "climate.x", "max_setpoint": 88.0},
            )
            payload = json.loads(_text(await server.call_tool("list_thermostat_configs", {})))
            assert [c["thermostat_entity_id"] for c in payload] == ["climate.x"]
            assert payload[0]["max_setpoint"] == 88.0
        finally:
            await conn.close()


class TestSetThermostatCycleTimeout:
    async def test_cycle_timeout_hours_is_stored(self):
        conn = await _conn()
        try:
            server = build_server(conn)
            result = await server.call_tool(
                "set_thermostat_config",
                {"thermostat_entity_id": "climate.x", "cycle_timeout_hours": 1.5},
            )
            assert "Updated config for climate.x" in _text(result)
            tc = await db.get_thermostat_config(conn, "climate.x")
            assert tc.cycle_timeout_hours == 1.5
        finally:
            await conn.close()

    async def test_cycle_timeout_hours_is_not_a_temperature(self):
        """It is a duration: identical in Celsius mode, and never in the temp echo."""
        conn = await _conn("C")
        try:
            server = build_server(conn)
            result = await server.call_tool(
                "set_thermostat_config",
                {"thermostat_entity_id": "climate.x", "cycle_timeout_hours": 4.0},
            )
            assert "cycle_timeout_hours=" not in _text(result)
            tc = await db.get_thermostat_config(conn, "climate.x")
            assert tc.cycle_timeout_hours == 4.0
        finally:
            await conn.close()

    async def test_omitting_cycle_timeout_hours_leaves_the_stored_value(self):
        conn = await _conn()
        try:
            server = build_server(conn)
            await server.call_tool(
                "set_thermostat_config",
                {"thermostat_entity_id": "climate.x", "cycle_timeout_hours": 6.0},
            )
            await server.call_tool(
                "set_thermostat_config",
                {"thermostat_entity_id": "climate.x", "has_bypass_damper": True},
            )
            tc = await db.get_thermostat_config(conn, "climate.x")
            assert tc.cycle_timeout_hours == 6.0
            assert tc.has_bypass_damper is True
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# mcp_server.main() — the stdio entry point
# ---------------------------------------------------------------------------


async def _is_closed(conn: aiosqlite.Connection) -> bool:
    """True when *conn* can no longer serve queries (aiosqlite closes hard)."""
    try:
        await conn.execute("SELECT 1")
    except Exception:
        return True
    return False


def _patch_entry_point(monkeypatch, tmp_path, *, boom: Exception | None = None) -> dict:
    """Point ``main()`` at a temp DATA_DIR and stub out the stdio transport.

    Everything else — makedirs, the filename migration, the real aiosqlite
    connection, ``db.init_db``, and the real ``build_server`` registration — runs
    for real, so the recorded connection is the one ``main()`` must close.
    """
    data_dir = tmp_path / "mcp-data"
    monkeypatch.setattr(mcp_server, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(mcp_server, "DB_PATH", str(data_dir / "app.db"))

    recorded: dict = {"conns": [], "runs": 0}
    real_build = mcp_server.build_server

    def build(conn):
        recorded["conns"].append(conn)
        server = real_build(conn)

        async def run_stdio_async():
            recorded["runs"] += 1
            if boom is not None:
                raise boom

        server.run_stdio_async = run_stdio_async
        return server

    monkeypatch.setattr(mcp_server, "build_server", build)
    recorded["data_dir"] = data_dir
    return recorded


class TestMcpServerMain:
    async def test_main_creates_the_data_dir_serves_stdio_then_closes(
        self, monkeypatch, tmp_path
    ):
        recorded = _patch_entry_point(monkeypatch, tmp_path)
        assert not recorded["data_dir"].exists()

        await mcp_server.main()

        assert recorded["data_dir"].is_dir()
        assert (recorded["data_dir"] / "app.db").exists()
        assert recorded["runs"] == 1
        assert len(recorded["conns"]) == 1
        assert await _is_closed(recorded["conns"][0])

    async def test_main_initialises_the_schema_and_uses_a_row_factory(
        self, monkeypatch, tmp_path
    ):
        """init_db must have run against the dedicated connection, not a stub."""
        recorded = _patch_entry_point(monkeypatch, tmp_path)
        await mcp_server.main()

        conn = recorded["conns"][0]
        assert conn.row_factory is aiosqlite.Row
        # Reopen the same file: the schema init_db performed is durable.
        verify = await aiosqlite.connect(str(recorded["data_dir"] / "app.db"))
        try:
            cur = await verify.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in await cur.fetchall()}
            assert "rooms" in tables
            assert "schedules" in tables
        finally:
            await verify.close()

    async def test_main_closes_the_connection_when_stdio_raises(self, monkeypatch, tmp_path):
        """The finally is the point of the try — a dead transport must not leak the handle."""
        recorded = _patch_entry_point(monkeypatch, tmp_path, boom=RuntimeError("stdio died"))

        with pytest.raises(RuntimeError, match="stdio died"):
            await mcp_server.main()

        assert recorded["runs"] == 1
        assert await _is_closed(recorded["conns"][0])

    async def test_main_migrates_a_legacy_flair_db_before_connecting(
        self, monkeypatch, tmp_path
    ):
        """DATA_DIR already holding flair.db is renamed to app.db, sidecars included."""
        recorded = _patch_entry_point(monkeypatch, tmp_path)
        data_dir = recorded["data_dir"]
        data_dir.mkdir()
        legacy = await aiosqlite.connect(str(data_dir / "flair.db"))
        try:
            await legacy.execute("CREATE TABLE legacy_marker (x INTEGER)")
            await legacy.commit()
        finally:
            await legacy.close()
        (data_dir / "flair.db-wal").write_bytes(b"")

        await mcp_server.main()

        assert not (data_dir / "flair.db").exists()
        assert (data_dir / "app.db").exists()
        assert (data_dir / "app.db-wal").exists()
        verify = await aiosqlite.connect(str(data_dir / "app.db"))
        try:
            cur = await verify.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='legacy_marker'"
            )
            assert await cur.fetchone() is not None
        finally:
            await verify.close()

    async def test_main_registers_the_full_tool_set_on_the_stdio_server(
        self, monkeypatch, tmp_path
    ):
        """build_server is called with the dedicated connection, not a fresh one."""
        captured: dict = {}
        recorded = _patch_entry_point(monkeypatch, tmp_path)
        real_build = mcp_server.build_server

        def build(conn):
            server = real_build(conn)
            captured["server"] = server
            return server

        monkeypatch.setattr(mcp_server, "build_server", build)

        await mcp_server.main()

        tools = {t.name for t in await captured["server"].list_tools()}
        assert {"list_rooms", "create_schedule", "set_thermostat_config"} <= tools
        assert await _is_closed(recorded["conns"][0])
