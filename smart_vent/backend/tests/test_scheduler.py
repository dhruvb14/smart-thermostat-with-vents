"""
Tests for the Scheduler module.

Covers:
  - _sync_engines: creating/removing engines based on room config
  - set_system_enabled: flag persistence, engine abort on disable
  - set_dev_mode: flag persistence, HA client wiring
  - _on_state_change: dispatch to correct engine on climate change
  - _handle_presence_event: presence → holdover → engine tick
  - get_all_zone_statuses: API status output
  - _tick_engine: room loading and tick invocation
  - get_temperature_unit / get_unit_change_ack_required / ack_unit_change
  - _startup_resolve_unit / _check_unit_change (Issue #123 Phase 1)
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from backend import db
from backend.models import Room
from backend.scheduler import _TICK_MAX_ATTEMPTS, Scheduler

from .integration.fake_ha import FakeHomeAssistant

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

THERMO_A = "climate.thermo_a"
THERMO_B = "climate.thermo_b"


def _make_ha() -> MagicMock:
    ha = MagicMock()
    ha.subscribe_all = MagicMock()
    ha.get_state.return_value = {
        "state": "cool",
        "attributes": {
            "current_temperature": 72.0,
            "temperature": 72.0,
            "hvac_action": "idle",
        },
    }
    ha.get_numeric_state.return_value = None
    ha.set_thermostat_temperature = AsyncMock()
    ha.open_cover = AsyncMock()
    ha.close_cover = AsyncMock()
    ha.dev_mode = False
    ha._dev_logger = None
    return ha


async def _setup_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    return conn


async def _insert_room(
    conn: aiosqlite.Connection,
    room_id: str,
    name: str,
    thermo: str,
) -> Room:
    room = Room(id=room_id, name=name, thermostat_entity_id=thermo)
    await db.upsert_room(conn, room)
    return room


def _make_scheduler(ha: MagicMock | None = None) -> Scheduler:
    if ha is None:
        ha = _make_ha()
    return Scheduler(ha=ha, db_path=":memory:")


# ---------------------------------------------------------------------------
# _sync_engines
# ---------------------------------------------------------------------------


class TestSyncEngines:
    @pytest.mark.asyncio
    async def test_creates_engine_for_each_thermostat(self):
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await _insert_room(conn, "r2", "Room 2", THERMO_B)

        await sched._sync_engines()

        assert THERMO_A in sched._engines
        assert THERMO_B in sched._engines
        assert len(sched._engines) == 2
        await conn.close()

    @pytest.mark.asyncio
    async def test_removes_engine_when_no_rooms(self):
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()
        assert THERMO_A in sched._engines

        # Remove the room → engine should be cleaned up
        await db.delete_room(conn, "r1")
        await sched._sync_engines()
        assert THERMO_A not in sched._engines
        await conn.close()

    @pytest.mark.asyncio
    async def test_idempotent_sync(self):
        """Running _sync_engines twice with same rooms doesn't duplicate."""
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()
        engine_1 = sched._engines[THERMO_A]
        await sched._sync_engines()
        engine_2 = sched._engines[THERMO_A]

        assert engine_1 is engine_2  # same instance, not recreated
        await conn.close()


# ---------------------------------------------------------------------------
# set_system_enabled / set_dev_mode
# ---------------------------------------------------------------------------


class TestSystemEnabled:
    @pytest.mark.asyncio
    async def test_enable_persists_to_db(self):
        sched = _make_scheduler()
        conn = await _setup_db()
        sched._db_conn = conn

        await sched.set_system_enabled(True)
        val = await db.get_system_setting(conn, "system_enabled", "0")
        assert val == "1"
        assert sched.get_system_enabled() is True
        await conn.close()

    @pytest.mark.asyncio
    async def test_disable_persists_to_db(self):
        sched = _make_scheduler()
        conn = await _setup_db()
        sched._db_conn = conn
        sched._engines = {}  # no engines to tick

        await sched.set_system_enabled(False)
        val = await db.get_system_setting(conn, "system_enabled", "1")
        assert val == "0"
        assert sched.get_system_enabled() is False
        await conn.close()

    @pytest.mark.asyncio
    async def test_disable_triggers_engine_ticks(self):
        """Disabling should tick all engines so they abort running cycles."""
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()

        # Mock the engine's tick to track it was called
        engine = sched._engines[THERMO_A]
        engine.tick = AsyncMock()
        engine.load_room_sensors = AsyncMock()

        await sched.set_system_enabled(False)

        engine.tick.assert_called_once()
        await conn.close()

    @pytest.mark.asyncio
    async def test_broadcast_on_change(self):
        broadcast = AsyncMock()
        ha = _make_ha()
        sched = Scheduler(ha=ha, db_path=":memory:", broadcast=broadcast)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._engines = {}

        await sched.set_system_enabled(False)

        broadcast.assert_called_with("system_enabled_changed", {"enabled": False})
        await conn.close()

    @pytest.mark.asyncio
    async def test_disable_closes_orphaned_cycle_logs(self):
        """Safety net: set_system_enabled(False) closes any DB cycle logs
        that are still open after engine ticks complete."""
        import json

        from backend.models import CycleLog

        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()

        # Insert an open cycle log directly in the DB (simulating a failed abort)
        cycle = CycleLog.create(
            thermostat_entity_id=THERMO_A,
            mode="cooling",
            rooms_json=json.dumps({"r1": {"name": "Room 1", "target": 74.0}}),
        )
        await db.insert_cycle_log(conn, cycle)

        # Mock engine tick to be a no-op (simulates abort that fails to close DB)
        engine = sched._engines[THERMO_A]
        engine.tick = AsyncMock()
        engine.load_room_sensors = AsyncMock()

        await sched.set_system_enabled(False)

        # The safety net should have closed the orphaned cycle log
        open_logs = await db.get_open_cycle_logs(conn, THERMO_A)
        assert len(open_logs) == 0, "Orphaned cycle log should be closed by safety net"
        await conn.close()

    @pytest.mark.asyncio
    async def test_disable_safety_net_handles_multiple_thermostats(self):
        """Safety net runs for every thermostat, not just the first."""
        import json

        from backend.models import CycleLog

        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await _insert_room(conn, "r2", "Room 2", THERMO_B)
        await sched._sync_engines()

        # Insert orphaned cycle logs for both thermostats
        for tid in [THERMO_A, THERMO_B]:
            cycle = CycleLog.create(
                thermostat_entity_id=tid,
                mode="cooling",
                rooms_json=json.dumps({}),
            )
            await db.insert_cycle_log(conn, cycle)

        for eng in sched._engines.values():
            eng.tick = AsyncMock()
            eng.load_room_sensors = AsyncMock()

        await sched.set_system_enabled(False)

        open_a = await db.get_open_cycle_logs(conn, THERMO_A)
        open_b = await db.get_open_cycle_logs(conn, THERMO_B)
        assert len(open_a) == 0, "Thermostat A orphan should be closed"
        assert len(open_b) == 0, "Thermostat B orphan should be closed"
        await conn.close()


class TestDevMode:
    @pytest.mark.asyncio
    async def test_enable_dev_mode(self):
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn

        await sched.set_dev_mode(True)

        assert sched.get_dev_mode() is True
        assert ha.dev_mode is True
        val = await db.get_system_setting(conn, "developer_mode", "0")
        assert val == "1"
        await conn.close()

    @pytest.mark.asyncio
    async def test_disable_dev_mode(self):
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn

        await sched.set_dev_mode(False)

        assert sched.get_dev_mode() is False
        assert ha.dev_mode is False
        val = await db.get_system_setting(conn, "developer_mode", "0")
        assert val == "0"
        await conn.close()

    @pytest.mark.asyncio
    async def test_dev_mode_broadcast(self):
        broadcast = AsyncMock()
        ha = _make_ha()
        sched = Scheduler(ha=ha, db_path=":memory:", broadcast=broadcast)
        conn = await _setup_db()
        sched._db_conn = conn

        await sched.set_dev_mode(True)

        broadcast.assert_called_with("dev_mode_changed", {"dev_mode": True})
        await conn.close()


# ---------------------------------------------------------------------------
# Reset + re-evaluate on every system/dev toggle
# ---------------------------------------------------------------------------


async def _insert_open_cycle(conn: aiosqlite.Connection, thermostat: str) -> str:
    """Insert an open (ended_at IS NULL) cycle log for a thermostat. Returns id."""
    import json

    from backend.models import CycleLog

    cycle = CycleLog.create(
        thermostat_entity_id=thermostat,
        mode="cooling",
        rooms_json=json.dumps({}),
    )
    await db.insert_cycle_log(conn, cycle)
    return cycle.id


class TestResetAndReevaluate:
    """Every system/dev toggle must (1) terminate open cycles and (2) tick
    every engine so it re-evaluates under the new flag state."""

    @pytest.mark.asyncio
    async def test_system_enable_closes_open_cycle(self):
        """System OFF → ON must not leave a stale ACTIVE cycle behind."""
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()
        await _insert_open_cycle(conn, THERMO_A)

        # Engine's _state is IDLE (no restore), so force_abort is a no-op; the
        # safety net is what catches this orphan. Mock tick to isolate.
        engine = sched._engines[THERMO_A]
        engine.tick = AsyncMock()
        engine.load_room_sensors = AsyncMock()

        await sched.set_system_enabled(True)

        open_logs = await db.get_open_cycle_logs(conn, THERMO_A)
        assert len(open_logs) == 0, "System enable must close orphaned cycles"
        engine.tick.assert_called_once()  # re-evaluation tick
        await conn.close()

    @pytest.mark.asyncio
    async def test_dev_mode_enable_closes_open_cycle(self):
        """Entering dev mode wipes any pre-existing open cycle so real and
        simulated cycles never coexist."""
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()
        await _insert_open_cycle(conn, THERMO_A)

        engine = sched._engines[THERMO_A]
        engine.tick = AsyncMock()
        engine.load_room_sensors = AsyncMock()

        await sched.set_dev_mode(True)

        open_logs = await db.get_open_cycle_logs(conn, THERMO_A)
        assert len(open_logs) == 0, "Dev ON must close orphaned cycles"
        engine.tick.assert_called_once()
        await conn.close()

    @pytest.mark.asyncio
    async def test_dev_mode_disable_closes_open_cycle(self):
        """Leaving dev mode must terminate any dev-mode-created cycle even if
        the system remains enabled (regression: this was the reported bug)."""
        ha = _make_ha()
        sched = _make_scheduler(ha)
        sched._dev_mode = True  # start in dev mode
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()
        await _insert_open_cycle(conn, THERMO_A)

        engine = sched._engines[THERMO_A]
        engine.tick = AsyncMock()
        engine.load_room_sensors = AsyncMock()

        await sched.set_dev_mode(False)

        open_logs = await db.get_open_cycle_logs(conn, THERMO_A)
        assert len(open_logs) == 0, "Dev OFF must close dev-mode cycles"
        engine.tick.assert_called_once()
        await conn.close()

    @pytest.mark.asyncio
    async def test_force_abort_called_on_running_engine(self):
        """When an engine has an in-flight cycle in memory, the toggle must
        invoke force_abort so engine state resets — not just DB cleanup."""
        from backend.engine.cycle_engine import CycleState

        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()

        engine = sched._engines[THERMO_A]
        # Simulate an in-flight cycle in memory.
        engine._state = CycleState.RUNNING
        engine.force_abort = AsyncMock()
        engine.tick = AsyncMock()
        engine.load_room_sensors = AsyncMock()

        await sched.set_dev_mode(True)

        engine.force_abort.assert_called_once()
        call_kwargs = engine.force_abort.call_args.kwargs
        assert "reason" in call_kwargs
        await conn.close()

    @pytest.mark.asyncio
    async def test_toggle_with_no_engines_is_noop(self):
        """Toggles must not crash when no engines are registered yet."""
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._engines = {}

        # None of these should raise.
        await sched.set_system_enabled(True)
        await sched.set_system_enabled(False)
        await sched.set_dev_mode(True)
        await sched.set_dev_mode(False)
        await conn.close()


# ---------------------------------------------------------------------------
# _on_state_change
# ---------------------------------------------------------------------------


class TestOnStateChange:
    @pytest.mark.asyncio
    async def test_climate_change_triggers_engine_tick(self):
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()

        engine = sched._engines[THERMO_A]
        engine.tick = AsyncMock()
        engine.load_room_sensors = AsyncMock()

        await sched._on_state_change(THERMO_A, {"state": "cool"})

        engine.tick.assert_called_once()
        await conn.close()

    @pytest.mark.asyncio
    async def test_non_climate_entity_ignored(self):
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()

        engine = sched._engines[THERMO_A]
        engine.tick = AsyncMock()
        engine.load_room_sensors = AsyncMock()

        # sensor change → should NOT trigger engine tick
        await sched._on_state_change("sensor.temperature", {"state": "72.5"})

        engine.tick.assert_not_called()
        await conn.close()

    @pytest.mark.asyncio
    async def test_unknown_climate_entity_ignored(self):
        """Climate entity with no engine → no crash."""
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._engines = {}

        # Should not raise
        await sched._on_state_change("climate.unknown", {"state": "cool"})
        await conn.close()

    @pytest.mark.asyncio
    async def test_presence_sensor_triggers_handling(self):
        """binary_sensor with state=on triggers presence handling."""
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()

        # Add a presence sensor for the room
        from backend.models import RoomPresenceSensor

        ps = RoomPresenceSensor.create(room_id="r1", entity_id="binary_sensor.pir_1")
        await db.add_room_presence_sensor(conn, ps)

        engine = sched._engines[THERMO_A]
        engine.handle_presence = AsyncMock()
        engine.tick = AsyncMock()
        engine.load_room_sensors = AsyncMock()

        await sched._on_state_change("binary_sensor.pir_1", {"state": "on"})

        engine.handle_presence.assert_called_once()
        await conn.close()

    @pytest.mark.asyncio
    async def test_presence_off_not_handled(self):
        """binary_sensor with state != 'on' is not treated as presence."""
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()

        from backend.models import RoomPresenceSensor

        ps = RoomPresenceSensor.create(room_id="r1", entity_id="binary_sensor.pir_1")
        await db.add_room_presence_sensor(conn, ps)

        engine = sched._engines[THERMO_A]
        engine.handle_presence = AsyncMock()
        engine.tick = AsyncMock()
        engine.load_room_sensors = AsyncMock()

        await sched._on_state_change("binary_sensor.pir_1", {"state": "off"})

        engine.handle_presence.assert_not_called()
        await conn.close()


# ---------------------------------------------------------------------------
# get_all_zone_statuses
# ---------------------------------------------------------------------------


class TestGetAllZoneStatuses:
    @pytest.mark.asyncio
    async def test_returns_status_for_each_engine(self):
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await _insert_room(conn, "r2", "Room 2", THERMO_B)
        await sched._sync_engines()

        statuses = sched.get_all_zone_statuses()
        assert len(statuses) == 2

        tids = {s["thermostat_entity_id"] for s in statuses}
        assert THERMO_A in tids
        assert THERMO_B in tids
        await conn.close()

    @pytest.mark.asyncio
    async def test_status_fields_present(self):
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()

        statuses = sched.get_all_zone_statuses()
        assert len(statuses) == 1
        s = statuses[0]
        assert "thermostat_entity_id" in s
        assert "cycle_state" in s
        assert "hvac_mode" in s
        assert "hvac_action" in s
        assert "current_temp" in s
        assert "rooms" in s
        await conn.close()

    @pytest.mark.asyncio
    async def test_empty_when_no_engines(self):
        sched = _make_scheduler()
        sched._engines = {}
        assert sched.get_all_zone_statuses() == []


# ---------------------------------------------------------------------------
# _tick_engine
# ---------------------------------------------------------------------------


class TestTickEngine:
    @pytest.mark.asyncio
    async def test_loads_rooms_and_ticks(self):
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()

        engine = sched._engines[THERMO_A]
        engine.load_room_sensors = AsyncMock()
        engine.tick = AsyncMock()

        await sched._tick_engine(THERMO_A, engine)

        engine.load_room_sensors.assert_called_once()
        engine.tick.assert_called_once_with(conn)
        await conn.close()

    @pytest.mark.asyncio
    async def test_tick_exception_does_not_propagate(self):
        """Engine tick failure should be caught, not crash scheduler."""
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()

        engine = sched._engines[THERMO_A]
        engine.load_room_sensors = AsyncMock()
        engine.tick = AsyncMock(side_effect=RuntimeError("boom"))

        # Should not raise
        await sched._tick_engine(THERMO_A, engine)
        await conn.close()

    @pytest.mark.asyncio
    async def test_tick_exception_written_to_event_logger(self):
        """Issue #57: tick failures must surface to the UI Live Feed, not just
        container logs. Scheduler mirrors the exception to event_logger so the
        user can see vent/HA errors from inside the app."""
        from backend.event_logger import EventLogger

        ha = _make_ha()
        event_logger = EventLogger()
        sched = Scheduler(ha=ha, db_path=":memory:", event_logger=event_logger)
        conn = await _setup_db()
        sched._db_conn = conn
        event_logger.set_conn(conn)
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()

        engine = sched._engines[THERMO_A]
        engine.load_room_sensors = AsyncMock()
        engine.tick = AsyncMock(side_effect=RuntimeError("set_cover_position failed"))

        with patch("asyncio.sleep", AsyncMock()):  # skip retry backoff
            await sched._tick_engine(THERMO_A, engine)

        events = await db.get_event_logs(conn, category="engine", limit=10)
        tick_errors = [e for e in events if e["level"] == "error" and "Tick failed" in e["message"]]
        assert len(tick_errors) == 1
        assert THERMO_A in tick_errors[0]["message"]
        assert "set_cover_position failed" in tick_errors[0]["message"]
        # Logged only once, after all retries are exhausted — not per attempt.
        assert engine.tick.await_count == _TICK_MAX_ATTEMPTS
        await conn.close()

    @pytest.mark.asyncio
    async def test_pretick_db_error_is_retried_then_recovers(self):
        """A transient pre-tick failure (e.g. 'database is locked') must be
        retried, not silently dropped — the zone keeps being controlled. The
        pre-tick reads used to sit outside the try, so the exception vanished
        into _tick_all's gather(return_exceptions=True). (Issue #286)"""
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()
        engine = sched._engines[THERMO_A]
        engine.load_room_sensors = AsyncMock()
        engine.tick = AsyncMock()

        real_get_rooms = db.get_rooms_for_thermostat
        calls = {"n": 0}

        async def flaky_get_rooms(c, tid):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")
            return await real_get_rooms(c, tid)

        with (
            patch("asyncio.sleep", AsyncMock()),
            patch.object(db, "get_rooms_for_thermostat", flaky_get_rooms),
        ):
            await sched._tick_engine(THERMO_A, engine)

        assert calls["n"] == 2  # failed once, retried and succeeded
        engine.tick.assert_awaited_once_with(conn)
        await conn.close()

    @pytest.mark.asyncio
    async def test_pretick_db_error_exhausts_retries_and_logs(self):
        """A persistent pre-tick failure surfaces to the Live Feed after the
        retries are exhausted, rather than being swallowed silently. (#286)"""
        from backend.event_logger import EventLogger

        ha = _make_ha()
        event_logger = EventLogger()
        sched = Scheduler(ha=ha, db_path=":memory:", event_logger=event_logger)
        conn = await _setup_db()
        sched._db_conn = conn
        event_logger.set_conn(conn)
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()
        engine = sched._engines[THERMO_A]
        engine.load_room_sensors = AsyncMock()
        engine.tick = AsyncMock()

        async def always_locked(c, tid):
            raise RuntimeError("database is locked")

        with (
            patch("asyncio.sleep", AsyncMock()),
            patch.object(db, "get_rooms_for_thermostat", always_locked),
        ):
            await sched._tick_engine(THERMO_A, engine)  # must not raise

        engine.tick.assert_not_awaited()  # pre-tick read never succeeded
        events = await db.get_event_logs(conn, category="engine", limit=10)
        tick_errors = [e for e in events if e["level"] == "error" and "Tick failed" in e["message"]]
        assert len(tick_errors) == 1
        assert "database is locked" in tick_errors[0]["message"]
        assert f"after {_TICK_MAX_ATTEMPTS} attempts" in tick_errors[0]["message"]
        await conn.close()


# ---------------------------------------------------------------------------
# Temperature unit detection & persistence (Issue #123 Phase 1)
# ---------------------------------------------------------------------------


@pytest.fixture
async def unit_scheduler(tmp_path):
    """A fully-started Scheduler backed by FakeHomeAssistant for unit tests."""
    fake_ha = FakeHomeAssistant()
    sched = Scheduler(ha=fake_ha, db_path=str(tmp_path / "unit.db"))
    await sched.start()
    yield sched
    await sched.stop()


class TestTemperatureUnitMethods:
    async def test_get_temperature_unit_returns_F_by_default(self, unit_scheduler):
        assert unit_scheduler.get_temperature_unit() == "F"

    async def test_get_unit_change_ack_required_false_by_default(self, unit_scheduler):
        assert await unit_scheduler.get_unit_change_ack_required() is False

    async def test_ack_unit_change_clears_flag(self, unit_scheduler):
        await db.set_system_setting(unit_scheduler._db_conn, "unit_change_ack_required", "1")
        assert await unit_scheduler.get_unit_change_ack_required() is True
        await unit_scheduler.ack_unit_change()
        assert await unit_scheduler.get_unit_change_ack_required() is False

    async def test_env_var_override_sets_C(self, tmp_path):
        sched = Scheduler(ha=FakeHomeAssistant(), db_path=str(tmp_path / "c.db"))
        with patch.dict(os.environ, {"TEMPERATURE_UNIT": "C"}):
            await sched.start()
        try:
            assert sched.get_temperature_unit() == "C"
            assert sched._unit_override == "C"
        finally:
            await sched.stop()

    async def test_env_var_override_F(self, tmp_path):
        sched = Scheduler(ha=FakeHomeAssistant(), db_path=str(tmp_path / "f.db"))
        with patch.dict(os.environ, {"TEMPERATURE_UNIT": "F"}):
            await sched.start()
        try:
            assert sched.get_temperature_unit() == "F"
        finally:
            await sched.stop()


class TestStartupResolveUnit:
    async def test_resolves_unit_from_ha(self, tmp_path):
        sched = Scheduler(ha=FakeHomeAssistant(), db_path=str(tmp_path / "r.db"))
        await sched.start()
        try:
            await asyncio.sleep(0.05)
            assert sched.get_temperature_unit() == "F"
        finally:
            await sched.stop()

    async def test_env_override_blocks_ha_resolution(self, tmp_path):
        sched = Scheduler(ha=FakeHomeAssistant(), db_path=str(tmp_path / "r2.db"))
        with patch.dict(os.environ, {"TEMPERATURE_UNIT": "C"}):
            await sched.start()
        try:
            await asyncio.sleep(0.05)
            assert sched.get_temperature_unit() == "C"
        finally:
            await sched.stop()

    async def test_blank_env_var_does_not_lock_and_auto_detects(self, tmp_path):
        # run.sh exports TEMPERATURE_UNIT="" when the add-on option is left blank
        # ("leave blank to auto-detect"). An empty string must behave like unset
        # — auto-detect from HA — not be treated as an override lock to °F.
        # (Issue #281 root cause 2)
        fake_ha = FakeHomeAssistant()
        fake_ha.ha_temp_unit = "C"  # a metric Home Assistant
        sched = Scheduler(ha=fake_ha, db_path=str(tmp_path / "blank.db"))
        with patch.dict(os.environ, {"TEMPERATURE_UNIT": ""}):
            await sched.start()
        try:
            assert sched._unit_override == ""  # blank is not a lock
            await asyncio.sleep(0.05)  # let _startup_resolve_unit run
            assert sched.get_temperature_unit() == "C"  # resolved from HA, not °F
        finally:
            await sched.stop()

    async def test_ha_failure_does_not_crash(self, tmp_path):
        fake_ha = FakeHomeAssistant()
        fake_ha.get_temperature_unit = AsyncMock(side_effect=RuntimeError("HA down"))
        sched = Scheduler(ha=fake_ha, db_path=str(tmp_path / "r3.db"))
        await sched.start()
        try:
            await asyncio.sleep(0.05)
            assert sched.get_temperature_unit() in ("F", "C")
        finally:
            await sched.stop()


class TestCheckUnitChange:
    async def test_change_detected_sets_ack_flag(self, tmp_path):
        fake_ha = FakeHomeAssistant()
        sched = Scheduler(ha=fake_ha, db_path=str(tmp_path / "c1.db"))
        await sched.start()
        try:
            await asyncio.sleep(0.05)
            fake_ha.get_temperature_unit = AsyncMock(return_value="C")
            await sched._check_unit_change()
            assert await sched.get_unit_change_ack_required() is True
        finally:
            await sched.stop()

    async def test_no_change_does_not_set_flag(self, tmp_path):
        fake_ha = FakeHomeAssistant()
        fake_ha.get_temperature_unit = AsyncMock(return_value="F")
        sched = Scheduler(ha=fake_ha, db_path=str(tmp_path / "c2.db"))
        await sched.start()
        try:
            await asyncio.sleep(0.05)
            await sched._check_unit_change()
            assert await sched.get_unit_change_ack_required() is False
        finally:
            await sched.stop()

    async def test_env_override_skips_ha_check(self, tmp_path):
        call_count = 0

        async def counting_unit():
            nonlocal call_count
            call_count += 1
            return "C"

        fake_ha = FakeHomeAssistant()
        fake_ha.get_temperature_unit = counting_unit
        sched = Scheduler(ha=fake_ha, db_path=str(tmp_path / "c3.db"))
        with patch.dict(os.environ, {"TEMPERATURE_UNIT": "F"}):
            await sched.start()
        try:
            call_count = 0
            await sched._check_unit_change()
            assert call_count == 0
        finally:
            await sched.stop()

    async def test_ha_failure_during_check_is_swallowed(self, tmp_path):
        fake_ha = FakeHomeAssistant()
        fake_ha.get_temperature_unit = AsyncMock(side_effect=RuntimeError("conn lost"))
        sched = Scheduler(ha=fake_ha, db_path=str(tmp_path / "c4.db"))
        await sched.start()
        try:
            await sched._check_unit_change()  # must not raise
        finally:
            await sched.stop()
