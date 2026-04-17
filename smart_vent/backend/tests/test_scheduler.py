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
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from backend import db
from backend.models import Room
from backend.scheduler import Scheduler

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
