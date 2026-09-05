"""
Cycle-engine coverage gaps — the cycle START / UPDATE / MONITOR paths.

Companion to ``test_cycle_engine.py``; this file targets the defensive and
rarely-taken branches of ``_do_tick``'s monitor dispatch,
``_start_or_update_cycle``, ``_close_idle_room_vents`` and ``_monitor_rooms``:

  * the "no valid cycle mode" monitor guard (#48 Bug 6)
  * orphan / cross-thermostat cycle-log cleanup at fresh start (#48 Bug 4)
  * diagnostics writes (vent events, temp samples) that must never fail a tick
  * the min-runtime-hold demand re-check and the rooms_json merge (#423)
  * the overflow-room skip in the idle-vent close sweep (#422)
  * the airflow-floor branch of the last-vent-to-close decision
  * the #427 self-repair returning None (termination stays blocked)

Every temperature here is °F — the engine never converts (see CLAUDE.md).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from backend import db
from backend.engine.cycle_engine import CycleEngine, CycleState
from backend.engine.room_manager import ActiveRoom
from backend.engine.vent_controller import VentController
from backend.models import (
    CycleLog,
    Room,
    RoomCycleState,
    RoomVent,
    Schedule,
    ThermostatConfig,
)

THERMO_ID = "climate.test_thermostat"
OTHER_THERMO_ID = "climate.other_thermostat"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_ha(
    ambient: float = 72.0,
    hvac_mode: str = "cool",
    hvac_action: str = "cooling",
    cover_state: str = "open",
) -> MagicMock:
    """Mock HAClient whose ``get_state`` routes cover.* separately from the
    thermostat, so the airflow-floor arithmetic (which reads real vent state)
    behaves like a live zone."""
    thermo = {
        "state": hvac_mode,
        "attributes": {
            "current_temperature": ambient,
            "temperature": ambient,
            "hvac_action": hvac_action,
        },
    }
    ha = MagicMock()
    ha.ha_temp_unit = "F"

    def _get_state(entity_id: str):
        if entity_id.startswith("cover."):
            return {"state": cover_state, "attributes": {}}
        return thermo

    ha.get_state.side_effect = _get_state
    ha.get_numeric_state.return_value = None
    ha.get_state_age_seconds.return_value = None
    ha.set_thermostat_temperature = AsyncMock()
    ha.set_thermostat_hvac_mode = AsyncMock()
    ha.open_cover = AsyncMock()
    ha.close_cover = AsyncMock()
    ha.call_service = AsyncMock()
    return ha


def _make_engine(ha: MagicMock, logger: AsyncMock | None = None) -> CycleEngine:
    return CycleEngine(
        thermostat_entity_id=THERMO_ID,
        ha=ha,
        vent_ctrl=VentController(ha),
        event_logger=logger,
        get_enabled=lambda: True,
        get_vacation_mode=lambda: False,
    )


def _tc(**overrides) -> ThermostatConfig:
    base: dict = {
        "thermostat_entity_id": THERMO_ID,
        "overshoot_delta": 2.0,
        "deadband": 0.5,
        "min_setpoint": 60.0,
        "max_setpoint": 85.0,
    }
    base.update(overrides)
    return ThermostatConfig(**base)


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    return conn


async def _add_room(
    conn: aiosqlite.Connection,
    room_id: str,
    name: str,
    *,
    thermostat: str = THERMO_ID,
    vents: list[str] | None = None,
) -> Room:
    room = Room.create(name=name, thermostat_entity_id=thermostat)
    room.id = room_id
    await db.upsert_room(conn, room)
    for entity_id in vents or []:
        await db.add_room_vent(conn, RoomVent.create(room_id, entity_id))
    return room


def _ar(room: Room, target: float = 74.0, source: str = "schedule") -> ActiveRoom:
    return ActiveRoom(room=room, target_temp=target, source=source)


async def _running_engine(
    ha: MagicMock,
    conn: aiosqlite.Connection,
    rooms: dict[str, ActiveRoom],
    *,
    mode: str = "cooling",
    logger: AsyncMock | None = None,
) -> tuple[CycleEngine, CycleLog]:
    """An engine parked in RUNNING with an open cycle log for ``rooms``."""
    engine = _make_engine(ha, logger)
    cycle = CycleLog.create(
        thermostat_entity_id=THERMO_ID,
        mode=mode,
        rooms_json=json.dumps(
            {rid: {"name": ar.room.name, "target": ar.target_temp} for rid, ar in rooms.items()}
        ),
    )
    await db.insert_cycle_log(conn, cycle)
    engine._state = CycleState.RUNNING
    engine._cycle_log = cycle
    engine._cycle_mode = mode
    engine._cycle_ha_mode = "cool" if mode == "cooling" else "heat"
    engine._active_rooms = dict(rooms)
    engine._room_cycle_states = {}
    for rid, ar in rooms.items():
        rcs = RoomCycleState(cycle_id=cycle.id, room_id=rid, target_temp=ar.target_temp)
        engine._room_cycle_states[rid] = rcs
        await db.upsert_room_cycle_state(conn, rcs)
    return engine, cycle


# ---------------------------------------------------------------------------
# _do_tick: the "no valid cycle mode" monitor guard (Issue #48 Bug 6)
# ---------------------------------------------------------------------------


class TestMonitorModeGuard:
    """A RUNNING cycle whose ``_cycle_mode`` is None (a restore edge case) must
    NOT be monitored against the live ``hvac_mode``: an "off"/"unknown" mode
    makes ``_is_at_target`` compare in an arbitrary direction, closing vents on
    rooms that never reached target."""

    @pytest.mark.asyncio
    async def test_tick_skips_monitoring_when_no_valid_cycle_mode(self, caplog):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            room = await _add_room(conn, "r1", "Bedroom")
            await db.upsert_schedule(
                conn,
                Schedule.create(
                    room_id="r1",
                    days_of_week=list(range(7)),
                    start_time=time(0, 0),
                    end_time=time(23, 59),
                    target_temp=72.0,
                ),
            )
            # Thermostat is reachable but idle in a mode the engine cannot read
            # a direction from, and the restored cycle lost its locked mode.
            ha = _make_ha(ambient=72.0, hvac_mode="off", hvac_action="off")
            engine, _cycle = await _running_engine(
                ha, conn, {"r1": _ar(room, target=72.0)}, mode="cooling"
            )
            engine._cycle_mode = None
            engine._monitor_rooms = AsyncMock()

            with caplog.at_level(logging.WARNING, logger="backend.engine.cycle_engine"):
                await engine._do_tick(conn)

            engine._monitor_rooms.assert_not_awaited()
            assert any("Skipping room monitoring" in r.message for r in caplog.records), (
                "the guard must say why it declined to monitor"
            )
            # The cycle itself is untouched — the guard defers, it does not abort.
            assert engine.cycle_state == CycleState.RUNNING
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_tick_monitors_when_the_locked_mode_survived(self, caplog):
        """Control for the test above: with ``_cycle_mode`` intact the same
        tick DOES monitor, so the guard is discriminating on the mode and not
        on some unrelated part of the setup."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            room = await _add_room(conn, "r1", "Bedroom")
            await db.upsert_schedule(
                conn,
                Schedule.create(
                    room_id="r1",
                    days_of_week=list(range(7)),
                    start_time=time(0, 0),
                    end_time=time(23, 59),
                    target_temp=72.0,
                ),
            )
            ha = _make_ha(ambient=72.0, hvac_mode="off", hvac_action="off")
            engine, _cycle = await _running_engine(
                ha, conn, {"r1": _ar(room, target=72.0)}, mode="cooling"
            )
            engine._monitor_rooms = AsyncMock()

            with caplog.at_level(logging.WARNING, logger="backend.engine.cycle_engine"):
                await engine._do_tick(conn)

            engine._monitor_rooms.assert_awaited_once()
            assert engine._monitor_rooms.await_args.args[1] == "cooling"
            assert not any("Skipping room monitoring" in r.message for r in caplog.records)
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _start_or_update_cycle: fresh-start cleanup of stale cycle logs
# ---------------------------------------------------------------------------


class TestFreshStartCycleLogCleanup:
    @pytest.mark.asyncio
    async def test_orphaned_open_log_is_closed_and_announced(self):
        """A crash (or a kill -9) between the cycle-log insert and the state
        transition leaves an open row behind; the next fresh start must close
        it so the UI shows one Active cycle, not two."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            room = await _add_room(conn, "r1", "Bedroom")
            orphan = CycleLog.create(
                thermostat_entity_id=THERMO_ID, mode="cooling", rooms_json="{}"
            )
            await db.insert_cycle_log(conn, orphan)

            logger = AsyncMock()
            engine = _make_engine(_make_ha(ambient=80.0), logger)
            await engine._start_or_update_cycle(conn, {"r1": _ar(room)}, "cooling")

            open_logs = await db.get_open_cycle_logs(conn, THERMO_ID)
            assert [c.id for c in open_logs] == [engine._cycle_log.id], (
                "only the brand-new cycle may remain open"
            )
            closed = await db.get_cycle_log(conn, orphan.id)
            assert closed.ended_at is not None
            messages = [c.args[2] for c in logger.log.await_args_list]
            assert any("orphaned cycle log" in m for m in messages)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_no_orphan_means_no_warning(self):
        """Guards the ``orphaned > 0`` condition itself: a clean start writes
        no orphan warning at all."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            room = await _add_room(conn, "r1", "Bedroom")
            logger = AsyncMock()
            engine = _make_engine(_make_ha(ambient=80.0), logger)
            await engine._start_or_update_cycle(conn, {"r1": _ar(room)}, "cooling")

            messages = [c.args[2] for c in logger.log.await_args_list]
            assert not any("orphaned cycle log" in m for m in messages)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_open_cycle_on_another_thermostat_holding_our_room_is_closed(self):
        """Issue #48 Bug 4: a room reassigned between thermostats can still sit
        in the old zone's open cycle. Starting here must close that cycle so the
        room is never conditioned by two zones at once."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            room = await _add_room(conn, "r1", "Bedroom")
            stale = CycleLog.create(
                thermostat_entity_id=OTHER_THERMO_ID, mode="cooling", rooms_json="{}"
            )
            await db.insert_cycle_log(conn, stale)
            await db.upsert_room_cycle_state(
                conn, RoomCycleState(cycle_id=stale.id, room_id="r1", target_temp=74.0)
            )

            logger = AsyncMock()
            engine = _make_engine(_make_ha(ambient=80.0), logger)
            await engine._start_or_update_cycle(conn, {"r1": _ar(room)}, "cooling")

            assert await db.get_open_cycle_logs(conn, OTHER_THERMO_ID) == []
            messages = [c.args[2] for c in logger.log.await_args_list]
            assert any("on other thermostats" in m for m in messages)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_other_thermostats_unrelated_cycle_is_left_alone(self):
        """The counterpart: another zone's cycle that does NOT contain our room
        must survive, and no cross-close warning is emitted."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            room = await _add_room(conn, "r1", "Bedroom")
            await _add_room(conn, "r9", "Elsewhere", thermostat=OTHER_THERMO_ID)
            theirs = CycleLog.create(
                thermostat_entity_id=OTHER_THERMO_ID, mode="cooling", rooms_json="{}"
            )
            await db.insert_cycle_log(conn, theirs)
            await db.upsert_room_cycle_state(
                conn, RoomCycleState(cycle_id=theirs.id, room_id="r9", target_temp=74.0)
            )

            logger = AsyncMock()
            engine = _make_engine(_make_ha(ambient=80.0), logger)
            await engine._start_or_update_cycle(conn, {"r1": _ar(room)}, "cooling")

            assert len(await db.get_open_cycle_logs(conn, OTHER_THERMO_ID)) == 1
            messages = [c.args[2] for c in logger.log.await_args_list]
            assert not any("on other thermostats" in m for m in messages)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_vent_event_write_failure_does_not_stop_the_cycle(self):
        """The ``opened_at_start`` rows are diagnostics only — a failing write
        must be swallowed so the cycle still reaches RUNNING."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            room = await _add_room(conn, "r1", "Bedroom", vents=["cover.vent_r1"])
            engine = _make_engine(_make_ha(ambient=80.0))

            with patch.object(
                db, "insert_cycle_vent_event", AsyncMock(side_effect=RuntimeError("locked"))
            ) as insert:
                await engine._start_or_update_cycle(conn, {"r1": _ar(room)}, "cooling")

            insert.assert_awaited()  # the write was attempted for the room's vent
            assert engine.cycle_state == CycleState.RUNNING
            assert engine._cycle_log is not None
            assert await db.get_cycle_log(conn, engine._cycle_log.id) is not None
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _start_or_update_cycle: mid-cycle update paths
# ---------------------------------------------------------------------------


class TestMidCycleUpdate:
    @pytest.mark.asyncio
    async def test_joined_room_without_a_reading_cannot_release_the_hold(self):
        """Issue #423 releases the min-runtime hold when joining demand is
        unsatisfied. A sensorless room has no demand to judge, so it must be
        skipped — not crash the update and not release the hold on a guess."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_cycle_runtime_min=10))
            r1 = await _add_room(conn, "r1", "Bedroom")
            r2 = await _add_room(conn, "r2", "Hallway")  # no sensors at all
            ha = _make_ha(ambient=72.0)
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(r1)})
            engine._cycle_log.in_min_runtime_hold = True
            await db.set_cycle_log_min_runtime_hold(conn, cycle.id, True)

            await engine._start_or_update_cycle(conn, {"r1": _ar(r1), "r2": _ar(r2)}, "cooling")

            assert "r2" in engine._active_rooms
            assert engine._cycle_log.in_min_runtime_hold is True, (
                "a room with no reading must not release the hold"
            )
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_joined_room_with_live_demand_releases_the_hold(self):
        """Control for the skip above — the same code path DOES release when
        the joining room has a reading that is off target."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_cycle_runtime_min=10))
            r1 = await _add_room(conn, "r1", "Bedroom")
            r2 = await _add_room(conn, "r2", "Office")
            ha = _make_ha(ambient=72.0)
            ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
                82.0 if eid == "sensor.r2" else None
            )
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(r1)})
            engine._sensor_map = {"r2": ["sensor.r2"]}
            engine._cycle_log.in_min_runtime_hold = True
            await db.set_cycle_log_min_runtime_hold(conn, cycle.id, True)

            await engine._start_or_update_cycle(conn, {"r1": _ar(r1), "r2": _ar(r2)}, "cooling")

            assert engine._cycle_log.in_min_runtime_hold is False
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_corrupt_rooms_json_is_rebuilt_not_propagated(self):
        """The snapshot merge must survive a malformed ``rooms_json`` (a
        truncated write, a hand-edited row) by starting from an empty dict."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            r1 = await _add_room(conn, "r1", "Bedroom")
            r2 = await _add_room(conn, "r2", "Office")
            engine, cycle = await _running_engine(_make_ha(ambient=72.0), conn, {"r1": _ar(r1)})
            engine._cycle_log.rooms_json = "{not valid json"

            await engine._start_or_update_cycle(conn, {"r1": _ar(r1), "r2": _ar(r2)}, "cooling")

            snapshot = json.loads(engine._cycle_log.rooms_json)
            assert set(snapshot) == {"r1", "r2"}
            assert snapshot["r2"]["name"] == "Office"
            # And the repaired snapshot was persisted, not just held in memory.
            reloaded = await db.get_cycle_log(conn, cycle.id)
            assert set(json.loads(reloaded.rooms_json)) == {"r1", "r2"}
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_non_object_rooms_json_is_rebuilt(self):
        """Valid JSON of the wrong shape (a list) parses fine but cannot be
        merged into — it must be discarded rather than blowing up the update."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            r1 = await _add_room(conn, "r1", "Bedroom")
            r2 = await _add_room(conn, "r2", "Office")
            engine, _cycle = await _running_engine(_make_ha(ambient=72.0), conn, {"r1": _ar(r1)})
            engine._cycle_log.rooms_json = "[]"

            await engine._start_or_update_cycle(conn, {"r1": _ar(r1), "r2": _ar(r2)}, "cooling")

            snapshot = json.loads(engine._cycle_log.rooms_json)
            assert isinstance(snapshot, dict)
            assert set(snapshot) == {"r1", "r2"}
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_no_room_or_trigger_change_records_a_plain_mode_reason(self):
        """An update pass with nothing added, removed or changed must not
        attribute the setpoint write to a room change — the cycle-detail view
        would then claim an in-place trigger update that never happened."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            r1 = await _add_room(conn, "r1", "Bedroom")
            engine, cycle = await _running_engine(_make_ha(ambient=80.0), conn, {"r1": _ar(r1)})

            # Same room, same trigger → added/removed/changed are all empty.
            await engine._start_or_update_cycle(conn, {"r1": _ar(r1)}, "cooling")

            history = await db.get_cycle_setpoint_history(conn, cycle.id)
            assert [h.reason for h in history] == ["mode=cooling"]
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_in_place_trigger_change_is_attributed(self):
        """Control for the reason above: a real trigger change is labelled."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            r1 = await _add_room(conn, "r1", "Bedroom")
            engine, cycle = await _running_engine(
                _make_ha(ambient=80.0), conn, {"r1": _ar(r1, target=74.0)}
            )

            await engine._start_or_update_cycle(conn, {"r1": _ar(r1, target=71.0)}, "cooling")

            history = await db.get_cycle_setpoint_history(conn, cycle.id)
            assert [h.reason for h in history] == ["trigger updated in place"]
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _close_idle_room_vents: the overflow exemption (#422)
# ---------------------------------------------------------------------------


class TestCloseIdleRoomVentsOverflowSkip:
    @pytest.mark.asyncio
    async def test_overflow_room_vents_are_not_closed_but_plain_idle_ones_are(self):
        """#422: overflow rooms (#237) are deliberately open during the
        min-runtime hold. The restore path repopulates the overflow set before
        this sweep runs, so closing them here silently defeated the feature."""
        conn = await _conn()
        try:
            tc = _tc(has_bypass_damper=True)  # no airflow floor in the way
            await db.upsert_thermostat_config(conn, tc)
            active = await _add_room(conn, "r1", "Bedroom", vents=["cover.vent_r1"])
            await _add_room(conn, "r_over", "Overflow", vents=["cover.vent_over"])
            await _add_room(conn, "r_idle", "Idle", vents=["cover.vent_idle"])

            ha = _make_ha(ambient=72.0)
            engine = _make_engine(ha)
            engine._active_rooms = {"r1": _ar(active)}
            engine._overflow_room_ids = {"r_over"}

            await engine._close_idle_room_vents(conn, tc)

            closed = {c.args[0] for c in ha.close_cover.await_args_list}
            assert "cover.vent_idle" in closed, "a plain idle room must still be closed"
            assert "cover.vent_over" not in closed, (
                "an overflow room's vent must survive the idle sweep"
            )
            assert "cover.vent_r1" not in closed
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _monitor_rooms: diagnostics writes and the floor/repair branches
# ---------------------------------------------------------------------------


class TestMonitorRoomsDiagnosticsResilience:
    @pytest.mark.asyncio
    async def test_force_reopen_event_write_failure_is_contained(self):
        """The max-vent-closed watchdog reopened a vent; mirroring that into
        the diagnostics stream must never be able to fail the tick (the
        physical reopen has already happened)."""
        conn = await _conn()
        try:
            tc = _tc(max_vent_closed_min=5, min_cycle_runtime_min=0)
            await db.upsert_thermostat_config(conn, tc)
            r1 = await _add_room(conn, "r1", "Bedroom", vents=["cover.vent_r1"])
            ha = _make_ha(ambient=72.0, cover_state="closed")
            ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
                80.0 if eid == "sensor.r1" else None
            )
            engine, _cycle = await _running_engine(ha, conn, {"r1": _ar(r1)})
            engine._sensor_map = {"r1": ["sensor.r1"]}
            engine._room_vents = {"r1": await db.get_room_vents(conn, "r1")}
            # The vent has been shut far longer than max_vent_closed_min.
            engine._room_cycle_states["r1"].vent_closed_at = datetime.now(UTC) - timedelta(
                minutes=30
            )

            with patch.object(
                db, "insert_cycle_vent_event", AsyncMock(side_effect=RuntimeError("locked"))
            ) as insert:
                await engine._monitor_rooms(conn, "cooling")

            assert any(c.args[5] == "force_reopened_max_closed" for c in insert.await_args_list), (
                "the force-reopen event write must have been attempted"
            )
            # The watchdog's physical reopen still went out to HA.
            assert "cover.vent_r1" in {c.args[0] for c in ha.open_cover.await_args_list}
            assert engine.cycle_state == CycleState.RUNNING
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_temp_sample_write_failure_is_contained(self):
        """Per-tick temperature sampling feeds a chart; a failing insert must
        not stop the tick from monitoring and terminating the cycle."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(has_bypass_damper=True))
            r1 = await _add_room(conn, "r1", "Bedroom", vents=["cover.vent_r1"])
            ha = _make_ha(ambient=72.0)
            ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
                70.0 if eid == "sensor.r1" else None
            )
            engine, _cycle = await _running_engine(ha, conn, {"r1": _ar(r1)})
            engine._sensor_map = {"r1": ["sensor.r1"]}
            engine._room_vents = {"r1": await db.get_room_vents(conn, "r1")}

            with patch.object(
                db, "insert_cycle_temp_sample", AsyncMock(side_effect=RuntimeError("locked"))
            ) as insert:
                await engine._monitor_rooms(conn, "cooling")

            insert.assert_awaited_once()
            # 70 °F is at/below the 74 °F cooling target → the room was served
            # and the cycle completed despite the sampling failure.
            assert engine.cycle_state == CycleState.IDLE
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_closed_reached_target_event_failure_is_contained(self):
        """The vent has physically closed and ``vent_closed_at`` is committed;
        a failing diagnostics row must not undo either."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(has_bypass_damper=True))
            r1 = await _add_room(conn, "r1", "Bedroom", vents=["cover.vent_r1"])
            ha = _make_ha(ambient=72.0)
            ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
                70.0 if eid == "sensor.r1" else None
            )
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(r1)})
            engine._sensor_map = {"r1": ["sensor.r1"]}
            engine._room_vents = {"r1": await db.get_room_vents(conn, "r1")}
            rcs = engine._room_cycle_states["r1"]

            with patch.object(
                db, "insert_cycle_vent_event", AsyncMock(side_effect=RuntimeError("locked"))
            ) as insert:
                await engine._monitor_rooms(conn, "cooling")

            assert any(c.args[5] == "closed_reached_target" for c in insert.await_args_list)
            assert rcs.vent_closed_at is not None
            assert "cover.vent_r1" in {c.args[0] for c in ha.close_cover.await_args_list}
            states = {r.room_id: r for r in await db.get_room_cycle_states(conn, cycle.id)}
            assert states["r1"].vent_closed_at is not None, (
                "the close must still be persisted when the event row fails"
            )
        finally:
            await conn.close()


class TestLastVentCloseRespectsTheFloor:
    @pytest.mark.asyncio
    async def test_last_vent_closes_normally_when_the_floor_allows_it(self):
        """The bypass branch only exists for the deadlock case. When the zone
        has enough other open vents to satisfy the floor, the last cycle vent
        must go through the normal ``close_room_vents`` path — which re-checks
        the floor — rather than the unconditional force-close."""
        conn = await _conn()
        try:
            # 3 smart vents on the zone, half of which must stay open →
            # required = ceil(3 * 0.5) = 2, with no passive registers to
            # discount. Closing r1's single vent leaves 2 open, so the floor
            # is satisfied and no bypass is needed.
            tc = _tc(total_vents_count=3, min_open_vents_fraction=0.5)
            await db.upsert_thermostat_config(conn, tc)
            r1 = await _add_room(conn, "r1", "Bedroom", vents=["cover.vent_r1"])
            # An idle room contributes two more open vents to the zone pool.
            await _add_room(conn, "r_idle", "Idle", vents=["cover.vent_i1", "cover.vent_i2"])

            ha = _make_ha(ambient=72.0, cover_state="open")
            ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
                70.0 if eid == "sensor.r1" else None
            )
            engine, _cycle = await _running_engine(ha, conn, {"r1": _ar(r1)})
            engine._sensor_map = {"r1": ["sensor.r1"]}
            engine._room_vents = {"r1": await db.get_room_vents(conn, "r1")}

            with patch.object(VentController, "force_close_vents", AsyncMock()) as forced:
                await engine._monitor_rooms(conn, "cooling")

            # The floor was satisfied, so the deadlock bypass must not fire.
            forced.assert_not_awaited()
            assert "cover.vent_r1" in {c.args[0] for c in ha.close_cover.await_args_list}
            assert engine.cycle_state == CycleState.IDLE
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_last_vent_bypasses_the_floor_when_it_would_deadlock(self):
        """Control for the branch above (Bug 6): with no other open vents the
        floor can never be met, so the engine force-closes to let the cycle
        end instead of running forever at target."""
        conn = await _conn()
        try:
            tc = _tc(total_vents_count=1, min_open_vents_fraction=1.0)
            await db.upsert_thermostat_config(conn, tc)
            r1 = await _add_room(conn, "r1", "Bedroom", vents=["cover.vent_r1"])

            ha = _make_ha(ambient=72.0, cover_state="open")
            ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
                70.0 if eid == "sensor.r1" else None
            )
            logger = AsyncMock()
            engine, _cycle = await _running_engine(ha, conn, {"r1": _ar(r1)}, logger=logger)
            engine._sensor_map = {"r1": ["sensor.r1"]}
            engine._room_vents = {"r1": await db.get_room_vents(conn, "r1")}

            await engine._monitor_rooms(conn, "cooling")

            messages = [c.args[2] for c in logger.log.await_args_list]
            assert any("bypassing the airflow floor" in m for m in messages)
            assert engine.cycle_state == CycleState.IDLE
        finally:
            await conn.close()


class TestRepairFailureBlocksTermination:
    @pytest.mark.asyncio
    async def test_failed_self_repair_keeps_the_cycle_running(self):
        """#427: a room in the active map with no RoomCycleState is repaired in
        place. When the repair itself fails, the engine must fall back to the
        conservative behaviour — the room blocks termination for another tick
        rather than the cycle completing without ever conditioning it."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(has_bypass_damper=True))
            r1 = await _add_room(conn, "r1", "Bedroom", vents=["cover.vent_r1"])
            r2 = await _add_room(conn, "r2", "Office", vents=["cover.vent_r2"])
            ha = _make_ha(ambient=72.0)
            ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: 70.0
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(r1)})
            engine._sensor_map = {"r1": ["sensor.r1"], "r2": ["sensor.r2"]}
            engine._room_vents = {
                "r1": await db.get_room_vents(conn, "r1"),
                "r2": await db.get_room_vents(conn, "r2"),
            }
            # r1 was already served earlier in the cycle, so on its own it
            # would let the cycle terminate on this tick.
            engine._room_cycle_states["r1"].vent_closed_at = datetime.now(UTC)
            # r2 is active but its cycle state never landed (the #427 zombie).
            engine._active_rooms["r2"] = _ar(r2)

            with patch.object(
                db, "upsert_room_cycle_state", AsyncMock(side_effect=RuntimeError("locked"))
            ):
                await engine._monitor_rooms(conn, "cooling")

            assert engine.cycle_state == CycleState.RUNNING, (
                "an unrepaired zombie room must block termination"
            )
            # Nothing was persisted for r2 — the repair really did fail.
            states = {r.room_id for r in await db.get_room_cycle_states(conn, cycle.id)}
            assert "r2" not in states
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_successful_self_repair_lets_the_cycle_proceed(self):
        """Control: when the repair succeeds the room gains a cycle state and
        its vents are opened, so it is monitored like any other room."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(has_bypass_damper=True))
            r1 = await _add_room(conn, "r1", "Bedroom", vents=["cover.vent_r1"])
            r2 = await _add_room(conn, "r2", "Office", vents=["cover.vent_r2"])
            ha = _make_ha(ambient=72.0, cover_state="closed")
            ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: 70.0
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(r1)})
            engine._sensor_map = {"r1": ["sensor.r1"], "r2": ["sensor.r2"]}
            engine._room_vents = {"r1": await db.get_room_vents(conn, "r1")}
            engine._active_rooms["r2"] = _ar(r2)

            await engine._monitor_rooms(conn, "cooling")

            # The repair looked up r2's vents (they were absent from the cached
            # map), created its cycle state, and opened them.
            states = {r.room_id: r for r in await db.get_room_cycle_states(conn, cycle.id)}
            assert "r2" in states
            assert "cover.vent_r2" in {c.args[0] for c in ha.open_cover.await_args_list}
        finally:
            await conn.close()
