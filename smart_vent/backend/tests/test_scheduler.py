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
from datetime import UTC, datetime, timedelta
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
    async def test_removed_engine_with_running_cycle_is_aborted(self):
        """Removing a thermostat's last room must abort its in-flight cycle —
        otherwise the HA thermostat keeps the overshoot setpoint. (Issue #285)"""
        from backend.engine.cycle_engine import CycleState

        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()
        engine = sched._engines[THERMO_A]
        engine._state = CycleState.RUNNING
        engine.force_abort = AsyncMock()

        await db.delete_room(conn, "r1")
        await sched._sync_engines()

        engine.force_abort.assert_called_once()
        assert engine.force_abort.call_args.kwargs.get("reason")
        assert THERMO_A not in sched._engines
        await conn.close()

    @pytest.mark.asyncio
    async def test_removed_engine_closes_open_cycle_log(self):
        """A removed thermostat's open cycle_log must be closed so it doesn't
        show as a permanently 'Active' cycle in the UI/metrics. (Issue #285)"""
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()
        await _insert_open_cycle(conn, THERMO_A)
        assert await db.get_open_cycle_logs(conn, THERMO_A)  # open before removal

        await db.delete_room(conn, "r1")
        await sched._sync_engines()

        assert await db.get_open_cycle_logs(conn, THERMO_A) == []  # closed on removal
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

    @pytest.mark.asyncio
    async def test_a_failed_restore_is_contained_to_its_own_zone(self):
        """#604: engines are created in a loop and each is published only
        *after* its restore returns, so an exception out of restore_from_db
        used to cost every other thermostat's engine as well as the process —
        this loop runs inside aiohttp's on_startup. One poisoned zone must now
        cost only its own restored state: every other zone still gets a
        published engine, and the failing zone gets a *cold* one so its next
        tick starts a fresh cycle with the timeout monitor and reconciler
        supervising it again.
        """
        from backend.engine.cycle_engine import CycleEngine, CycleState

        ha = _make_ha()
        sched = _make_scheduler(ha)
        sched._event_logger = AsyncMock()
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await _insert_room(conn, "r2", "Room 2", THERMO_B)

        async def _restore(engine_self, _conn):
            if engine_self.thermostat_entity_id != THERMO_A:
                return
            # Half-restore, then fail: the discarded engine must not be the
            # one that gets published.
            engine_self._state = CycleState.RUNNING
            engine_self._cycle_mode = "cooling"
            raise AttributeError("'list' object has no attribute 'items'")

        with patch.object(CycleEngine, "restore_from_db", _restore):
            await sched._sync_engines()

        # Containment: the *other* zone was still created and published.
        assert set(sched._engines) == {THERMO_A, THERMO_B}
        # ...and the failing zone is published cold, not half-restored.
        poisoned = sched._engines[THERMO_A]
        assert poisoned.cycle_state == CycleState.IDLE
        assert poisoned._cycle_mode is None
        assert poisoned.thermostat_entity_id == THERMO_A
        # Logged loudly enough to reach the UI's Logs page, not just stderr.
        messages = [c.args[2] for c in sched._event_logger.log.await_args_list]
        assert any("Cycle restore failed" in m and THERMO_A in m for m in messages), messages
        await conn.close()

    @pytest.mark.asyncio
    async def test_a_failed_restore_without_an_event_logger_still_publishes(self, caplog):
        """The event-log write is optional wiring (``event_logger`` defaults to
        None in the local-dev/unit path), so the #604 containment must not
        depend on it. The python log still records the failure."""
        import logging

        from backend.engine.cycle_engine import CycleEngine, CycleState

        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()
        assert sched._event_logger is None

        await _insert_room(conn, "r1", "Room 1", THERMO_A)

        async def _restore(_engine_self, _conn):
            raise RuntimeError("poisoned snapshot")

        with (
            patch.object(CycleEngine, "restore_from_db", _restore),
            caplog.at_level(logging.ERROR, logger="backend.scheduler"),
        ):
            await sched._sync_engines()

        assert set(sched._engines) == {THERMO_A}
        assert sched._engines[THERMO_A].cycle_state == CycleState.IDLE
        assert any("Cycle restore failed for" in r.getMessage() for r in caplog.records), (
            caplog.text
        )
        await conn.close()

    @pytest.mark.asyncio
    async def test_a_failed_restore_keeps_the_lockout_and_closes_the_open_row(self):
        """The degraded path must not trade one hazard for two others (#604).

        Driven by a real, reachable failure rather than a patched-out restore:
        an open row whose ``started_at`` will not parse, which raises out of
        ``db.get_open_cycle_logs`` — i.e. *after* restore_from_db's first block
        has already rehydrated the compressor off-time lockout. Two properties
        the replacement engine owes the zone:

        * The #432 lockout survives. A cold engine's ``_last_cycle_ended_at``
          is None and ``_in_offtime_lockout`` is then unconditionally False, so
          without carrying it across, a restart 30 s after a cycle ended would
          re-enable an immediate compressor restart inside the 10-minute
          protection window that #432 exists to preserve across restarts.
        * The open cycle_logs row is closed, not orphaned. The cold engine is
          IDLE with no ``_cycle_log``, and both the timeout monitor and the
          reconciler act only on the engine's own ``_cycle_log`` — so an open
          row left behind is supervised by nobody, renders as a permanently
          "Active" cycle on the Logs page, and bills the whole idle gap as
          runtime whenever it finally gets closed.
        """
        from backend.engine.cycle_engine import CycleState
        from backend.models import CycleLog, ThermostatConfig

        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        tc = ThermostatConfig(thermostat_entity_id=THERMO_A, min_cycle_offtime_min=10)
        await db.upsert_thermostat_config(conn, tc)

        # A cycle that ended 30 s ago — well inside the 10-minute lockout.
        ended = datetime.now(UTC) - timedelta(seconds=30)
        await db.insert_cycle_log(
            conn,
            CycleLog(
                id="closed-cycle",
                thermostat_entity_id=THERMO_A,
                started_at=ended - timedelta(minutes=12),
                mode="cooling",
                rooms_json="{}",
            ),
        )
        await db.close_cycle_log(conn, "closed-cycle", ended)
        # ...and an open row the restore cannot read at all.
        await conn.execute(
            "INSERT INTO cycle_logs (id, thermostat_entity_id, started_at, mode, rooms_json) "
            "VALUES (?, ?, ?, ?, ?)",
            ("open-cycle", THERMO_A, "not-a-date", "cooling", "{}"),
        )
        await conn.commit()

        await sched._sync_engines()

        engine = sched._engines[THERMO_A]
        assert engine.cycle_state == CycleState.IDLE
        assert engine._last_cycle_ended_at == ended, (
            "the rehydrated off-time clock must be carried onto the replacement"
        )
        assert engine._in_offtime_lockout(tc) is True, (
            "short-cycle protection must not be silently disabled by the degrade"
        )
        assert await db.get_open_cycle_logs(conn, THERMO_A) == [], (
            "the unresumable open row must be closed, not orphaned"
        )
        await conn.close()

    @pytest.mark.asyncio
    async def test_a_failed_restore_survives_a_failing_cycle_log_cleanup(self, caplog):
        """The orphan-row cleanup is itself best-effort: if closing the row
        fails too, the engine must still be published — re-raising here would
        undo the containment and take on_startup down anyway."""
        import logging

        from backend.engine.cycle_engine import CycleEngine, CycleState

        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()
        await _insert_room(conn, "r1", "Room 1", THERMO_A)

        async def _restore(_engine_self, _conn):
            raise RuntimeError("poisoned snapshot")

        async def _boom(*_a, **_kw):
            raise aiosqlite.OperationalError("database is locked")

        with (
            patch.object(CycleEngine, "restore_from_db", _restore),
            patch.object(db, "close_open_cycle_logs", _boom),
            caplog.at_level(logging.ERROR, logger="backend.scheduler"),
        ):
            await sched._sync_engines()

        assert sched._engines[THERMO_A].cycle_state == CycleState.IDLE
        assert any("Cycle-log cleanup failed" in r.getMessage() for r in caplog.records), (
            caplog.text
        )
        await conn.close()

    @pytest.mark.asyncio
    async def test_a_successful_restore_publishes_the_restored_engine(self):
        """Control for the two tests above (#604): when restore succeeds the
        engine that was restored is the one published — the containment must
        not quietly throw away good restored state."""
        from backend.engine.cycle_engine import CycleEngine, CycleState

        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        await _insert_room(conn, "r1", "Room 1", THERMO_A)

        async def _restore(engine_self, _conn):
            engine_self._state = CycleState.RUNNING
            engine_self._cycle_mode = "cooling"

        with patch.object(CycleEngine, "restore_from_db", _restore):
            await sched._sync_engines()

        assert sched._engines[THERMO_A].cycle_state == CycleState.RUNNING
        assert sched._engines[THERMO_A]._cycle_mode == "cooling"
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
        """Toggles must not crash when no engines are registered yet — and the
        flag must still be applied in memory AND persisted, because
        `_reset_and_reevaluate`'s early return must not short-circuit the rest
        of the setter."""
        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._engines = {}

        for enabled in (True, False):
            await sched.set_system_enabled(enabled)
            assert sched.get_system_enabled() is enabled
            stored = await db.get_system_setting(conn, "system_enabled", "")
            assert stored == ("1" if enabled else "0")

        for dev in (True, False):
            await sched.set_dev_mode(dev)
            assert sched.get_dev_mode() is dev
            stored = await db.get_system_setting(conn, "developer_mode", "")
            assert stored == ("1" if dev else "0")
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
        """A climate entity with no engine of its own must not crash — and must
        not fan the event out to the engines that DO exist (the dispatch is a
        per-entity lookup, not a broadcast)."""
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

        await sched._on_state_change("climate.unknown", {"state": "cool"})

        engine.tick.assert_not_called()
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

        # Issue #286: the failure is retried the full budget before being
        # swallowed — a bare try/except that gave up on the first exception
        # would leave the zone uncontrolled for a whole tick.
        assert engine.tick.call_count == _TICK_MAX_ATTEMPTS
        assert engine.load_room_sensors.call_count == _TICK_MAX_ATTEMPTS
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

    async def test_clears_stuck_ack_flag_when_unit_now_matches(self, tmp_path):
        """Issue #288: the restart that applies the new unit must clear a banner
        the user never dismissed — once the resolved unit matches stored, the
        ack flag is cleared."""
        fake_ha = FakeHomeAssistant()
        fake_ha.ha_temp_unit = "C"
        sched = Scheduler(ha=fake_ha, db_path=str(tmp_path / "stuck.db"))
        await sched.start()
        try:
            await asyncio.sleep(0.05)  # let the start-up resolution settle
            # Pre-restart stuck state: banner up, stored still on the old unit.
            await db.set_system_setting(sched._db_conn, "temperature_unit", "F")
            await db.set_system_setting(sched._db_conn, "unit_change_ack_required", "1")
            # The applying restart resolves the unit from HA (now C).
            await sched._startup_resolve_unit()
            assert sched.get_temperature_unit() == "C"
            assert await sched.get_unit_change_ack_required() is False
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
        """A dropped HA connection mid-check must not raise — and must not
        leave the unit-change banner half-raised: the handler returns before
        comparing against the stored unit, so no ack flag is set."""
        fake_ha = FakeHomeAssistant()
        fake_ha.get_temperature_unit = AsyncMock(side_effect=RuntimeError("conn lost"))
        sched = Scheduler(ha=fake_ha, db_path=str(tmp_path / "c4.db"))
        await sched.start()
        try:
            await sched._check_unit_change()  # must not raise
            assert await sched.get_unit_change_ack_required() is False
        finally:
            await sched.stop()

    async def test_ack_not_undone_by_next_tick(self, tmp_path):
        """Issue #288: dismissing the banner must stick. The next tick sees the
        same (already-acknowledged) HA unit and must NOT re-raise the flag."""
        fake_ha = FakeHomeAssistant()
        sched = Scheduler(ha=fake_ha, db_path=str(tmp_path / "ack1.db"))
        await sched.start()
        try:
            await asyncio.sleep(0.05)  # _startup_resolve_unit → stored = "F"
            fake_ha.get_temperature_unit = AsyncMock(return_value="C")
            await sched._check_unit_change()
            assert await sched.get_unit_change_ack_required() is True
            # User dismisses.
            await sched.ack_unit_change()
            assert await sched.get_unit_change_ack_required() is False
            # Next tick — same C unit, still acknowledged → must stay dismissed.
            await sched._check_unit_change()
            assert await sched.get_unit_change_ack_required() is False
        finally:
            await sched.stop()

    async def test_reflags_on_genuinely_new_change_after_resolution(self, tmp_path):
        """Once the mismatch resolves (HA matches stored), the ack marker clears
        so a later, genuinely new change re-raises the banner."""
        fake_ha = FakeHomeAssistant()
        sched = Scheduler(ha=fake_ha, db_path=str(tmp_path / "reflag.db"))
        await sched.start()
        try:
            await asyncio.sleep(0.05)  # stored = "F"
            fake_ha.get_temperature_unit = AsyncMock(return_value="C")
            await sched._check_unit_change()
            await sched.ack_unit_change()
            assert await sched.get_unit_change_ack_required() is False
            # HA returns to F (matches stored) — mismatch resolved, marker cleared.
            fake_ha.get_temperature_unit = AsyncMock(return_value="F")
            await sched._check_unit_change()
            assert await sched.get_unit_change_ack_required() is False
            # HA switches to C again — a new change must re-raise the banner.
            fake_ha.get_temperature_unit = AsyncMock(return_value="C")
            await sched._check_unit_change()
            assert await sched.get_unit_change_ack_required() is True
        finally:
            await sched.stop()


class TestBackgroundTasks:
    """Issue #304: fire-and-forget startup tasks must keep a strong reference so
    the event loop's weak references don't let them be garbage-collected mid-run."""

    async def test_spawn_bg_tracks_then_cleans_up(self):
        sched = _make_scheduler()
        ran = asyncio.Event()

        async def work():
            ran.set()

        task = sched._spawn_bg(work())
        # Strong reference held immediately so it can't be GC'd before running.
        assert task in sched._bg_tasks
        await task
        assert ran.is_set()
        # The done callback removes the (now-finished) task from the set.
        assert task not in sched._bg_tasks

    async def test_startup_unit_resolution_task_is_tracked(self, tmp_path):
        fake_ha = FakeHomeAssistant()
        sched = Scheduler(ha=fake_ha, db_path=str(tmp_path / "bg.db"))
        gate = asyncio.Event()

        async def slow_resolve():
            await gate.wait()

        sched._startup_resolve_unit = slow_resolve
        with patch.dict(os.environ, {"TEMPERATURE_UNIT": ""}):
            await sched.start()
        try:
            # The pending unit-resolution task is held strongly, not GC-eligible.
            assert any(not t.done() for t in sched._bg_tasks)
        finally:
            gate.set()
            await sched.stop()


class TestRestoreThenPublish:
    """#428: restore_from_db runs outside the engine lock — an engine must not
    be reachable (in self._engines) until its restore has completed, or a
    climate state-change tick can interleave mid-restore and have its fresh
    cycle clobbered by the stale snapshot."""

    @pytest.mark.asyncio
    async def test_engine_not_published_during_restore(self):
        from backend.engine.cycle_engine import CycleEngine

        ha = _make_ha()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()
        await _insert_room(conn, "r1", "Room 1", THERMO_A)

        published_during_restore: list[bool] = []
        original = CycleEngine.restore_from_db

        async def spy(self_engine, c):
            published_during_restore.append(self_engine.thermostat_entity_id in sched._engines)
            return await original(self_engine, c)

        with patch.object(CycleEngine, "restore_from_db", spy):
            await sched._sync_engines()

        assert published_during_restore == [False], (
            "the engine must be invisible to tick dispatch until restore completes"
        )
        assert THERMO_A in sched._engines, "and published once restore is done"
        await conn.close()


class TestReloadDbRebuildsEngines:
    """#430: POST /api/restore swaps the DB under running engines. Surviving
    engines kept in-memory cycles pointing at rows that no longer exist in
    the restored data — their history writes silently failed and the
    backup's own open cycles were never adopted. reload_db must abort every
    engine against the OLD connection, then rebuild from scratch."""

    @pytest.mark.asyncio
    async def test_running_engines_aborted_and_rebuilt(self, tmp_path):
        from backend.engine.cycle_engine import CycleState

        ha = _make_ha()
        sched = _make_scheduler(ha)
        db_file = tmp_path / "app.db"
        sched._db_path = str(db_file)
        conn = await aiosqlite.connect(str(db_file))
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()
        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()

        old_engine = sched._engines[THERMO_A]
        old_engine._state = CycleState.RUNNING
        old_engine.force_abort = AsyncMock()

        await sched.reload_db()

        # The pre-swap engine was aborted against the old world...
        old_engine.force_abort.assert_awaited_once()
        # ...and the map was rebuilt from the restored data with a NEW engine.
        assert THERMO_A in sched._engines
        assert sched._engines[THERMO_A] is not old_engine
        await sched._db_conn.close()


# ---------------------------------------------------------------------------
# Coverage additions: reload/abort failure resilience, unit helpers under HA
# errors, sweep + purge branches, holdover refresh skips, rollup job wrappers
# ---------------------------------------------------------------------------


class TestReloadDbAbortFailure:
    @pytest.mark.asyncio
    async def test_engine_abort_failure_does_not_block_reload(self):
        """A failing force_abort during restore must be logged and swallowed —
        the reload must still swap connections and clear the engine map."""
        sched = _make_scheduler()
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()
        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()
        engine = sched._engines[THERMO_A]
        engine.force_abort = AsyncMock(side_effect=RuntimeError("db is gone"))

        await sched.reload_db()

        engine.force_abort.assert_awaited_once()
        assert sched._db_conn is not conn  # fresh connection
        # Engines were rebuilt from the (empty) restored DB.
        assert sched._engines == {}
        await sched._db_conn.close()


class TestAckUnitChangeHaFailure:
    @pytest.mark.asyncio
    async def test_ha_error_clears_flag_but_not_acked_unit(self):
        ha = _make_ha()
        ha.get_temperature_unit = AsyncMock(side_effect=RuntimeError("HA down"))
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn
        await db.set_system_setting(conn, "unit_change_ack_required", "1")

        await sched.ack_unit_change()

        assert await db.get_system_setting(conn, "unit_change_ack_required", "0") == "0"
        # The acked unit must NOT be recorded when HA could not be asked.
        assert await db.get_system_setting(conn, "unit_change_acked_unit", "") == ""
        await conn.close()


class TestUnitResolutionHaUnavailable:
    @pytest.mark.asyncio
    async def test_startup_resolve_gives_up_on_timeout(self):
        ha = _make_ha()
        ha.wait_connected = AsyncMock(side_effect=TimeoutError)
        ha.get_temperature_unit = AsyncMock()
        sched = _make_scheduler(ha)
        conn = await _setup_db()
        sched._db_conn = conn

        await sched._startup_resolve_unit()

        ha.get_temperature_unit.assert_not_awaited()
        assert await db.get_system_setting(conn, "temperature_unit", "F") == "F"
        await conn.close()

    @pytest.mark.asyncio
    async def test_check_unit_change_skipped_while_disconnected(self, tmp_path):
        """Per-tick unit check must not query HA while the WS is down."""
        fake_ha = FakeHomeAssistant()
        called = []
        original = fake_ha.get_temperature_unit

        async def _tracking():
            called.append(1)
            return await original()

        fake_ha.get_temperature_unit = _tracking
        sched = Scheduler(ha=fake_ha, db_path=str(tmp_path / "u.db"))
        await sched.start()
        try:
            fake_ha._connected.clear()  # simulate the HA WebSocket dropping
            called.clear()  # ignore any startup-time resolution calls
            await sched._check_unit_change()
            assert called == []  # returned before asking HA
        finally:
            await sched.stop()


class TestReevaluateFailureResilience:
    @pytest.mark.asyncio
    async def test_abort_and_cleanup_failures_do_not_stop_reevaluation(self):
        """Per-engine abort/cleanup failures must not prevent the remaining
        engines from being processed and re-ticked."""
        sched = _make_scheduler()
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()
        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await _insert_room(conn, "r2", "Room 2", THERMO_B)
        await sched._sync_engines()

        bad = sched._engines[THERMO_A]
        good = sched._engines[THERMO_B]
        bad.force_abort = AsyncMock(side_effect=RuntimeError("abort boom"))
        good.force_abort = AsyncMock()
        sched._tick_engine = AsyncMock()

        with patch.object(
            db, "close_open_cycle_logs", AsyncMock(side_effect=RuntimeError("cleanup boom"))
        ):
            await sched._reset_and_reevaluate("test")

        good.force_abort.assert_awaited_once()  # unaffected by the bad engine
        assert sched._tick_engine.await_count == 2  # both re-evaluated
        await conn.close()


class TestEngineRemovalFailureResilience:
    @pytest.mark.asyncio
    async def test_removal_proceeds_when_abort_and_cleanup_fail(self):
        """Even if abort and cycle-log cleanup both blow up, the dead engine
        must still leave the map (a zombie engine keeps commanding HA)."""
        sched = _make_scheduler()
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()
        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        await sched._sync_engines()
        engine = sched._engines[THERMO_A]
        engine.force_abort = AsyncMock(side_effect=RuntimeError("abort boom"))

        await db.delete_room(conn, "r1")
        with patch.object(
            db, "close_open_cycle_logs", AsyncMock(side_effect=RuntimeError("cleanup boom"))
        ):
            await sched._sync_engines()

        assert THERMO_A not in sched._engines
        await conn.close()


class TestRollupJobWrappers:
    @pytest.mark.asyncio
    async def test_daily_job_delegates(self):
        sched = _make_scheduler()
        sched.run_daily_metrics_rollup = AsyncMock(return_value=0)
        await sched._rollup_daily_metrics_job()
        sched.run_daily_metrics_rollup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_monthly_job_delegates(self):
        sched = _make_scheduler()
        sched.run_monthly_metrics_rollup = AsyncMock(return_value=0)
        await sched._rollup_monthly_metrics_job()
        sched.run_monthly_metrics_rollup.assert_awaited_once()


class TestPurgeOldLogs:
    @pytest.mark.asyncio
    async def test_purge_logs_completion_logged(self, caplog):
        import logging
        from datetime import UTC, datetime, timedelta

        from backend.models import CycleLog

        sched = _make_scheduler()
        conn = await _setup_db()
        sched._db_conn = conn
        # A cycle well past the 30-day default retention.
        old_start = datetime.now(UTC) - timedelta(days=90)
        log_ = CycleLog(
            id="old1",
            thermostat_entity_id=THERMO_A,
            started_at=old_start,
            mode="cooling",
            rooms_json="{}",
        )
        await db.insert_cycle_log(conn, log_)
        await db.close_cycle_log(
            conn, "old1", ended_at=old_start + timedelta(minutes=10), ended_reason="completed"
        )

        with caplog.at_level(logging.INFO, logger="backend.scheduler"):
            await sched._purge_old_logs()

        async with conn.execute("SELECT COUNT(*) AS n FROM cycle_logs") as cur:
            row = await cur.fetchone()
        assert row["n"] == 0
        assert any("Log purge complete" in r.message for r in caplog.records)
        await conn.close()


class TestSweepExpiredSchedules:
    @pytest.mark.asyncio
    async def test_no_db_conn_is_noop(self):
        """Before the DB is opened the sweep must bail out *before* touching
        it — not merely survive by luck."""
        sched = _make_scheduler()
        sched._db_conn = None
        loader = AsyncMock(return_value=[])
        with patch.object(db, "get_expiring_schedules", loader):
            await sched._sweep_expired_schedules()  # must not raise
        loader.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_load_failure_logged_and_swallowed(self, caplog):
        import logging

        sched = _make_scheduler()
        conn = await _setup_db()
        sched._db_conn = conn
        with (
            patch.object(
                db, "get_expiring_schedules", AsyncMock(side_effect=RuntimeError("locked"))
            ),
            caplog.at_level(logging.ERROR, logger="backend.scheduler"),
        ):
            await sched._sweep_expired_schedules()
        assert any("Failed to load expiring schedules" in r.message for r in caplog.records)
        await conn.close()

    @pytest.mark.asyncio
    async def test_disable_failure_logged_and_schedule_left_enabled(self, caplog):
        import logging
        from datetime import time, timedelta

        from backend import tz
        from backend.models import Schedule

        sched = _make_scheduler()
        conn = await _setup_db()
        sched._db_conn = conn
        await _insert_room(conn, "r1", "Room 1", THERMO_A)
        # Expired yesterday; block on a weekday that is not today so it can
        # never be active "now" — a fixed wall-clock window would make this
        # test fail whenever CI happens to run inside it. Same clock as the
        # sweep (tz.now_local), same pattern as test_schedule_expiry_sweep.py.
        now = tz.now_local()
        expired = Schedule.create(
            room_id="r1",
            days_of_week=[(now.weekday() + 2) % 7],
            start_time=time(1, 0),
            end_time=time(2, 0),
            target_temp=70.0,
            expires_at=now.replace(tzinfo=None) - timedelta(days=1),
        )
        await db.upsert_schedule(conn, expired)

        with (
            patch.object(db, "upsert_schedule", AsyncMock(side_effect=RuntimeError("ro db"))),
            caplog.at_level(logging.ERROR, logger="backend.scheduler"),
        ):
            await sched._sweep_expired_schedules()

        assert any("Failed to auto-disable expired schedule" in r.message for r in caplog.records)
        # Still enabled in the DB — the sweep will retry next tick.
        fresh = await db.get_expiring_schedules(conn)
        assert [s.id for s in fresh] == [expired.id]
        await conn.close()


class TestRefreshContinuousPresenceSkips:
    @pytest.mark.asyncio
    async def test_zero_holdover_and_engineless_rooms_skipped(self):
        """Rooms with presence holdover disabled — and rooms whose thermostat
        has no engine — must be skipped without touching their sensors."""
        sched = _make_scheduler()
        conn = await _setup_db()
        sched._db_conn = conn
        sched._vent_ctrl = MagicMock()

        # r1: holdover disabled. r2: holdover on, but thermostat has no engine.
        r1 = Room(id="r1", name="NoHoldover", thermostat_entity_id=THERMO_A)
        r1.presence_holdover_hours = 0
        await db.upsert_room(conn, r1)
        r2 = Room(
            id="r2",
            name="NoEngine",
            thermostat_entity_id="climate.unmanaged",
            presence_holdover_hours=2.0,
        )
        await db.upsert_room(conn, r2)
        await sched._sync_engines()
        # Simulate the engine map lagging the room table (the race the guard
        # defends against): drop r2's engine after the sync.
        sched._engines.pop("climate.unmanaged")

        with patch.object(db, "get_room_presence_sensors", AsyncMock()) as sensors:
            await sched._refresh_continuous_presence()
        sensors.assert_not_awaited()  # both rooms skipped before the sensor read
        await conn.close()


class TestTickEventLoggerFailure:
    @pytest.mark.asyncio
    async def test_event_logger_failure_is_contained(self, caplog):
        """If writing the tick failure to the event log itself fails, the
        error must be logged and must not escape the tick loop."""
        import logging

        sched = _make_scheduler()
        conn = await _setup_db()
        sched._db_conn = conn
        sched._event_logger = MagicMock()
        sched._event_logger.log = AsyncMock(side_effect=RuntimeError("event log full"))

        engine = MagicMock()
        engine.load_room_sensors = AsyncMock()
        engine.tick = AsyncMock(side_effect=RuntimeError("tick boom"))

        with (
            patch("backend.scheduler._TICK_RETRY_BACKOFF_S", 0),
            caplog.at_level(logging.ERROR, logger="backend.scheduler"),
        ):
            await sched._tick_engine(THERMO_A, engine)  # must not raise

        assert engine.tick.await_count == _TICK_MAX_ATTEMPTS
        assert any(
            "Failed to write tick error to event logger" in r.message for r in caplog.records
        )
        await conn.close()
