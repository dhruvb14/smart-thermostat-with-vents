"""
Cycle-engine branch coverage — the untaken halves of the safety guards.

Companion to ``test_cycle_engine.py`` and ``test_cycle_engine_gaps_{a,b,c}.py``.
Statement coverage passed every one of these lines; branch coverage showed the
*other* direction of the guard was never exercised. Each test here asserts the
BEHAVIOUR of the untaken path — what setpoint was commanded, which vents moved,
whether the cycle terminated — not merely that a line ran.

Two families dominate:

  * **"engine wired without an event logger"** — every ``if self._logger:``
    guard exists because ``event_logger`` is an optional constructor argument
    (``CycleEngine(..., event_logger=None)``); the whole suite happened to pass
    one. The behaviour under test is that the safety decision itself (fail-open
    cooling, the compressor lockout, the cycle timeout, the orphan-log sweep,
    the airflow-floor refusal) is identical with the feed absent — the log is a
    narration, never a precondition.
  * **"the diagnostic side-car is missing"** — ``self._cycle_log`` is None while
    rooms are still being monitored/terminated (a restore edge case the code
    guards for), the thermostat state is unreadable, or a room has no vents.

Every temperature here is °F — the engine never converts (see CLAUDE.md).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock

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
    RoomSensor,
    RoomVent,
    Schedule,
    ThermostatConfig,
)

THERMO_ID = "climate.branch_thermostat"
OTHER_THERMO_ID = "climate.other_branch_thermostat"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_ha(
    ambient: float | None = 78.0,
    hvac_mode: str = "cool",
    hvac_action: str = "cooling",
    cover_state: str = "open",
    setpoint: float | None = None,
    sensors: dict[str, float] | None = None,
    thermostat_missing: bool = False,
) -> MagicMock:
    """Mock HAClient. ``get_state`` routes ``cover.*`` separately from the
    thermostat so the airflow-floor arithmetic sees real vent state, and
    ``get_numeric_state`` answers per-entity so room sensors and the
    outdoor-temperature entity can hold different readings."""
    attrs: dict = {"hvac_action": hvac_action}
    if ambient is not None:
        attrs["current_temperature"] = ambient
    sp = ambient if setpoint is None else setpoint
    if sp is not None:
        attrs["temperature"] = sp
    thermo = {"state": hvac_mode, "attributes": attrs}
    readings = dict(sensors or {})

    ha = MagicMock()
    ha.ha_temp_unit = "F"

    def _get_state(entity_id: str):
        if entity_id.startswith("cover."):
            return {"state": cover_state, "attributes": {}}
        if entity_id == THERMO_ID:
            return None if thermostat_missing else thermo
        return thermo

    def _get_numeric_state(entity_id: str, max_age_min: float | None = None):
        return readings.get(entity_id)

    ha.get_state.side_effect = _get_state
    ha.get_numeric_state.side_effect = _get_numeric_state
    ha.get_state_age_seconds.return_value = None
    ha.set_thermostat_temperature = AsyncMock()
    ha.set_thermostat_hvac_mode = AsyncMock()
    ha.open_cover = AsyncMock()
    ha.close_cover = AsyncMock()
    ha.call_service = AsyncMock()
    return ha


def _make_engine(
    ha: MagicMock,
    logger: AsyncMock | None = None,
    get_enabled=None,
) -> CycleEngine:
    return CycleEngine(
        thermostat_entity_id=THERMO_ID,
        ha=ha,
        vent_ctrl=VentController(ha),
        event_logger=logger,
        get_enabled=get_enabled if get_enabled is not None else (lambda: True),
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
    sensors: list[str] | None = None,
    **room_kwargs,
) -> Room:
    room = Room.create(name=name, thermostat_entity_id=thermostat, **room_kwargs)
    room.id = room_id
    await db.upsert_room(conn, room)
    for entity_id in vents or []:
        await db.add_room_vent(conn, RoomVent.create(room_id, entity_id))
    for entity_id in sensors or []:
        await db.add_room_sensor(conn, RoomSensor.create(room_id, entity_id))
    return room


async def _all_day_schedule(conn: aiosqlite.Connection, room_id: str, target: float) -> None:
    await db.upsert_schedule(
        conn,
        Schedule.create(
            room_id=room_id,
            days_of_week=list(range(7)),
            start_time=time(0, 0),
            end_time=time(23, 59),
            target_temp=target,
        ),
    )


def _ar(room: Room, target: float = 72.0, source: str = "schedule") -> ActiveRoom:
    return ActiveRoom(room=room, target_temp=target, source=source)


async def _running_engine(
    ha: MagicMock,
    conn: aiosqlite.Connection,
    rooms: dict[str, ActiveRoom],
    *,
    mode: str = "cooling",
    logger: AsyncMock | None = None,
) -> tuple[CycleEngine, CycleLog]:
    """An engine parked in RUNNING over ``rooms``, with an open cycle log."""
    engine = _make_engine(ha, logger)
    engine._state = CycleState.RUNNING
    engine._cycle_mode = mode
    engine._cycle_ha_mode = "cool" if mode == "cooling" else "heat"
    engine._active_rooms = dict(rooms)
    engine._sensor_map = {rid: [f"sensor.{rid}"] for rid in rooms}

    cycle = CycleLog.create(
        thermostat_entity_id=THERMO_ID,
        mode=mode,
        rooms_json=json.dumps(
            {rid: {"name": ar.room.name, "target": ar.target_temp} for rid, ar in rooms.items()}
        ),
    )
    await db.insert_cycle_log(conn, cycle)
    engine._cycle_log = cycle

    engine._room_cycle_states = {}
    engine._room_vents = {}
    for rid, ar in rooms.items():
        engine._room_vents[rid] = await db.get_room_vents(conn, rid)
        rcs = RoomCycleState(cycle_id=cycle.id, room_id=rid, target_temp=ar.target_temp)
        engine._room_cycle_states[rid] = rcs
        await db.upsert_room_cycle_state(conn, rcs)
    return engine, cycle


async def _open_cycle_count(conn: aiosqlite.Connection, thermostat: str) -> int:
    cur = await conn.execute(
        "SELECT COUNT(*) FROM cycle_logs WHERE thermostat_entity_id = ? AND ended_at IS NULL",
        (thermostat,),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _cycle_count(conn: aiosqlite.Connection) -> int:
    cur = await conn.execute("SELECT COUNT(*) FROM cycle_logs")
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _vent_event_count(conn: aiosqlite.Connection) -> int:
    cur = await conn.execute("SELECT COUNT(*) FROM cycle_vent_events")
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# _do_tick: the idle-reconcile gate re-reads the system-enabled flag (418->425)
# ---------------------------------------------------------------------------


class TestIdleReconcileEnabledGate:
    """``_do_tick`` reads ``_get_enabled()`` twice — once at the top guard and
    again before the no-demand idle reconcile — with several awaits in
    between. If the operator flips System Off during that window, the second
    read must win: no safety command, no vent reconcile."""

    @pytest.mark.asyncio
    async def test_system_disabled_between_the_two_reads_skips_the_safety_backstop(self):
        conn = await _conn()
        try:
            # Ambient 88°F is 3°F over the ceiling, so an enabled tick would
            # command the backstop (see the control below).
            await db.upsert_thermostat_config(conn, _tc(max_setpoint=85.0))
            ha = _make_ha(ambient=88.0)
            reads: list[bool] = [True, False]

            def _enabled() -> bool:
                return reads.pop(0)

            engine = _make_engine(ha, get_enabled=_enabled)
            await engine._do_tick(conn)

            assert reads == [], "both enabled reads must have happened"
            ha.set_thermostat_temperature.assert_not_awaited()
            ha.open_cover.assert_not_awaited()
            assert engine.cycle_state == CycleState.IDLE
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_still_enabled_on_the_second_read_enforces_the_ceiling(self):
        """Control: identical setup, flag still on — the backstop fires."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(max_setpoint=85.0))
            ha = _make_ha(ambient=88.0)
            engine = _make_engine(ha, get_enabled=lambda: True)

            await engine._do_tick(conn)

            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 85.0, hvac_mode="cool"
            )
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _do_tick: the cooling lockout without an event logger (479->519, 500->514)
# ---------------------------------------------------------------------------


class TestCoolingLockoutWithoutEventLogger:
    @pytest.mark.asyncio
    async def test_fail_open_starts_the_cooling_cycle_with_no_logger(self, caplog):
        """A configured lockout whose outdoor sensor is unreadable fails OPEN.
        With ``event_logger=None`` the operator loses the feed entry, but the
        cooling cycle must still start."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(cooling_lockout_below_f=55.0))
            room = await _add_room(conn, "r1", "Study", vents=["cover.r1"])
            await _all_day_schedule(conn, "r1", 72.0)
            # No outside_temperature_entity_id set → sensor_unavailable.
            ha = _make_ha(ambient=78.0, sensors={"sensor.r1": 80.0})
            engine = _make_engine(ha, logger=None)
            engine._sensor_map = {"r1": ["sensor.r1"]}

            with caplog.at_level(logging.WARNING, logger="backend.engine.cycle_engine"):
                await engine._do_tick(conn)

            assert any("fail-open" in r.message or "allowing" in r.message for r in caplog.records)
            assert engine.cycle_state == CycleState.RUNNING, (
                "fail-open means the cooling cycle starts anyway"
            )
            assert await _open_cycle_count(conn, THERMO_ID) == 1
            assert engine._cycle_mode == "cooling"
            assert room.id in engine._active_rooms
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_locked_out_suppresses_the_cycle_with_no_logger(self, caplog):
        """Outdoor 40°F is below the 55°F lockout: no cooling cycle may start,
        and the setpoint is parked at ambient + overshoot so the thermostat
        cannot restart the compressor on its own."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(cooling_lockout_below_f=55.0))
            await _add_room(conn, "r1", "Study", vents=["cover.r1"])
            await _all_day_schedule(conn, "r1", 72.0)
            await db.set_system_setting(conn, "outside_temperature_entity_id", "sensor.outside")
            ha = _make_ha(ambient=78.0, sensors={"sensor.r1": 80.0, "sensor.outside": 40.0})
            engine = _make_engine(ha, logger=None)
            engine._sensor_map = {"r1": ["sensor.r1"]}

            with caplog.at_level(logging.WARNING, logger="backend.engine.cycle_engine"):
                await engine._do_tick(conn)

            assert any("Cooling locked out" in r.message for r in caplog.records)
            assert engine.cycle_state == CycleState.IDLE
            assert await _cycle_count(conn) == 0, "no cycle log may be opened while locked out"
            # ambient 78 + overshoot 2 → parked at 80 (cooling side).
            ha.set_thermostat_temperature.assert_awaited_once_with(THERMO_ID, 80.0)
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _do_tick: every room filtered out while already IDLE (538->544)
# ---------------------------------------------------------------------------


class TestNoCompatibleRoomsWhileIdle:
    @pytest.mark.asyncio
    async def test_idle_zone_with_all_rooms_filtered_enforces_the_envelope_without_aborting(self):
        """The room's sensor says "heat" but the thermostat's own ambient
        (88°F) contradicts it, so the #38 sanity check corrects the mode to
        cooling — which then filters the heating room out. The engine is
        already IDLE, so there is nothing to abort; the #367 backstop must
        still drive the breached ceiling."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(max_setpoint=85.0))
            await _add_room(conn, "r1", "Basement", vents=["cover.r1"])
            await _all_day_schedule(conn, "r1", 72.0)
            ha = _make_ha(ambient=88.0, sensors={"sensor.r1": 60.0})
            engine = _make_engine(ha)
            engine._sensor_map = {"r1": ["sensor.r1"]}
            engine._abort_cycle = AsyncMock()

            await engine._do_tick(conn)

            engine._abort_cycle.assert_not_awaited(), "an IDLE engine has no cycle to abort"
            assert engine.cycle_state == CycleState.IDLE
            assert await _cycle_count(conn) == 0
            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 85.0, hvac_mode="cool"
            )
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _do_tick: compressor off-time lockout without a logger (561->574)
# ---------------------------------------------------------------------------


class TestOfftimeLockoutWithoutEventLogger:
    @pytest.mark.asyncio
    async def test_lockout_defers_the_new_cycle_with_no_logger(self, caplog):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_cycle_offtime_min=10))
            await _add_room(conn, "r1", "Study", vents=["cover.r1"])
            await _all_day_schedule(conn, "r1", 72.0)
            ha = _make_ha(ambient=78.0, sensors={"sensor.r1": 80.0})
            engine = _make_engine(ha, logger=None)
            engine._sensor_map = {"r1": ["sensor.r1"]}
            engine._last_cycle_ended_at = datetime.now(UTC) - timedelta(minutes=1)

            with caplog.at_level(logging.WARNING, logger="backend.engine.cycle_engine"):
                await engine._do_tick(conn)

            assert any("off-time lockout" in r.message for r in caplog.records)
            assert engine.cycle_state == CycleState.IDLE
            assert await _cycle_count(conn) == 0, "the compressor must not be restarted"
            ha.set_thermostat_temperature.assert_not_awaited()
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _do_tick: cycle timeout without a logger (677->687)
# ---------------------------------------------------------------------------


class TestCycleTimeoutWithoutEventLogger:
    @pytest.mark.asyncio
    async def test_timed_out_cycle_terminates_with_no_logger(self, caplog):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(cycle_timeout_hours=3.0))
            room = await _add_room(conn, "r1", "Study", vents=["cover.r1"])
            await _all_day_schedule(conn, "r1", 72.0)
            # 80°F against a 72°F cooling target — the room never reaches
            # target, so only the timeout can end this cycle.
            ha = _make_ha(ambient=78.0, sensors={"sensor.r1": 80.0})
            engine, cycle = await _running_engine(
                ha, conn, {"r1": _ar(room, target=72.0)}, logger=None
            )
            cycle.started_at = datetime.now(UTC) - timedelta(hours=4)

            with caplog.at_level(logging.WARNING, logger="backend.engine.cycle_engine"):
                await engine._do_tick(conn)

            assert any("timed out" in r.message for r in caplog.records)
            assert engine.cycle_state == CycleState.IDLE
            row = await db.get_cycle_log(conn, cycle.id)
            assert row is not None and row.ended_at is not None
            assert row.ended_reason == "timeout"
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _start_or_update_cycle: fresh-start log cleanup without a logger
# (908->924, 936->951)
# ---------------------------------------------------------------------------


class TestFreshStartCleanupWithoutEventLogger:
    @pytest.mark.asyncio
    async def test_orphan_and_cross_thermostat_logs_are_closed_with_no_logger(self, caplog):
        """Both sweeps narrate through the event feed; with no feed wired the
        DB cleanup itself must still happen, or the UI shows two "Active"
        cycles for one thermostat (#48 Bug 4)."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            room = await _add_room(conn, "r1", "Study", vents=["cover.r1"])

            orphan = CycleLog.create(THERMO_ID, "cooling", "{}")
            await db.insert_cycle_log(conn, orphan)
            cross = CycleLog.create(
                OTHER_THERMO_ID, "cooling", json.dumps({"r1": {"name": "Study", "target": 72.0}})
            )
            await db.insert_cycle_log(conn, cross)
            await db.upsert_room_cycle_state(
                conn, RoomCycleState(cycle_id=cross.id, room_id="r1", target_temp=72.0)
            )

            ha = _make_ha(ambient=78.0, sensors={"sensor.r1": 80.0})
            engine = _make_engine(ha, logger=None)
            engine._sensor_map = {"r1": ["sensor.r1"]}

            with caplog.at_level(logging.WARNING, logger="backend.engine.cycle_engine"):
                await engine._start_or_update_cycle(conn, {"r1": _ar(room)}, "cooling")

            assert any("orphaned open cycle log" in r.message for r in caplog.records)
            assert any("on other thermostats" in r.message for r in caplog.records)
            closed_orphan = await db.get_cycle_log(conn, orphan.id)
            closed_cross = await db.get_cycle_log(conn, cross.id)
            assert closed_orphan is not None and closed_orphan.ended_at is not None
            assert closed_cross is not None and closed_cross.ended_at is not None
            # Exactly one open cycle remains: the one just started.
            assert await _open_cycle_count(conn, THERMO_ID) == 1
            assert engine.cycle_state == CycleState.RUNNING
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _start_or_update_cycle: the removed-room loop (1084->1118, 1100->1116,
# 1120->1076)
# ---------------------------------------------------------------------------


class TestRemovedRoomLoop:
    @pytest.mark.asyncio
    async def test_removed_room_with_no_vents_is_dropped_without_touching_covers(self):
        """A sensor-only room (no smart vent) that goes idle mid-cycle must be
        dropped from the cycle without any cover traffic — and without
        consulting the airflow floor, which has nothing to weigh."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            keeper = await _add_room(conn, "r1", "Study", vents=["cover.r1"])
            ventless = await _add_room(conn, "r2", "Hall")  # no vents at all
            ha = _make_ha(ambient=78.0, sensors={"sensor.r1": 80.0, "sensor.r2": 80.0})
            engine, cycle = await _running_engine(
                ha,
                conn,
                {"r1": _ar(keeper), "r2": _ar(ventless)},
                logger=None,
            )
            engine._get_all_zone_vents = AsyncMock()

            await engine._start_or_update_cycle(conn, {"r1": _ar(keeper)}, "cooling")

            ha.close_cover.assert_not_awaited()
            (
                engine._get_all_zone_vents.assert_not_awaited(),
                ("no vents to close — the airflow floor is never consulted"),
            )
            assert "r2" not in engine._room_cycle_states
            assert set(engine._active_rooms) == {"r1"}
            snapshot = json.loads(cycle.rooms_json)
            assert "r2" in snapshot, "a removed room keeps its snapshot entry for the detail view"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_airflow_floor_keeps_a_removed_rooms_vent_open_with_no_logger(self, caplog):
        """Two smart vents, both open, a floor of 2: closing the removed
        room's vent would dead-head the duct, so it stays open even though the
        room left the cycle. Without an event logger the refusal is silent to
        the operator but identical in effect."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, _tc(total_vents_count=2, min_open_vents_fraction=1.0)
            )
            keeper = await _add_room(conn, "r1", "Study", vents=["cover.r1"])
            leaver = await _add_room(conn, "r2", "Den", vents=["cover.r2"])
            ha = _make_ha(
                ambient=78.0, cover_state="open", sensors={"sensor.r1": 80.0, "sensor.r2": 80.0}
            )
            engine, _cycle = await _running_engine(
                ha, conn, {"r1": _ar(keeper), "r2": _ar(leaver)}, logger=None
            )

            with caplog.at_level(logging.WARNING, logger="backend.engine.cycle_engine"):
                await engine._start_or_update_cycle(conn, {"r1": _ar(keeper)}, "cooling")

            assert any("airflow floor requires" in r.message for r in caplog.records)
            closed = [c.args[0] for c in ha.close_cover.await_args_list]
            assert "cover.r2" not in closed, "the floor must veto the close"
            assert "r2" not in engine._room_cycle_states
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _start_or_update_cycle: in-place trigger change with no RoomCycleState
# (1137->1150)
# ---------------------------------------------------------------------------


class TestInPlaceTriggerUpdateWithoutRoomState:
    @pytest.mark.asyncio
    async def test_changed_room_without_cycle_state_still_updates_the_active_map(self, caplog):
        """#427's zombie room — an active room whose ``RoomCycleState`` was lost
        — can also be the room whose trigger changes. The in-place update must
        not blow up on the missing row: the new target has to land in
        ``_active_rooms`` (that is what re-derives the setpoint) and no
        ``room_cycle_states`` row may be conjured for it here."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            room = await _add_room(conn, "r1", "Study", vents=["cover.r1"])
            ha = _make_ha(ambient=78.0, sensors={"sensor.r1": 80.0})
            engine, cycle = await _running_engine(
                ha, conn, {"r1": _ar(room, target=74.0, source="schedule")}
            )
            # The row is gone (the #286 locked-DB case) — mirror that in memory.
            engine._room_cycle_states.pop("r1")
            await conn.execute("DELETE FROM room_cycle_states WHERE room_id = 'r1'")

            with caplog.at_level(logging.INFO, logger="backend.engine.cycle_engine"):
                await engine._start_or_update_cycle(
                    conn, {"r1": _ar(room, target=70.0, source="override")}, "cooling"
                )

            assert any("trigger updated in place" in r.message for r in caplog.records)
            assert engine._active_rooms["r1"].target_temp == 70.0
            assert engine._active_rooms["r1"].source == "override"
            assert "r1" not in engine._room_cycle_states
            cur = await conn.execute("SELECT COUNT(*) FROM room_cycle_states WHERE room_id = 'r1'")
            assert (await cur.fetchone())[0] == 0
            assert engine.cycle_state == CycleState.RUNNING, "the cycle keeps running (#215)"
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _start_or_update_cycle: min-runtime hold survives a satisfied joiner
# (1189->1182)
# ---------------------------------------------------------------------------


class TestMinRuntimeHoldSurvivesSatisfiedJoin:
    @pytest.mark.asyncio
    async def test_room_joining_at_target_does_not_release_the_hold(self):
        """#423 releases the minimum-runtime hold only for rooms with real
        demand. A room that joins already AT its target adds no demand, so the
        hold — and with it the compressor's minimum runtime — must survive."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_cycle_runtime_min=10))
            held = await _add_room(conn, "r1", "Study", vents=["cover.r1"])
            joiner = await _add_room(conn, "r2", "Den", vents=["cover.r2"])
            # r2 reads 68°F against a 72°F cooling target — already satisfied.
            ha = _make_ha(ambient=78.0, sensors={"sensor.r1": 71.0, "sensor.r2": 68.0})
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(held, target=72.0)})
            cycle.in_min_runtime_hold = True
            await db.set_cycle_log_min_runtime_hold(conn, cycle.id, True)
            engine._sensor_map = {"r1": ["sensor.r1"], "r2": ["sensor.r2"]}

            await engine._start_or_update_cycle(
                conn,
                {"r1": _ar(held, target=72.0), "r2": _ar(joiner, target=72.0)},
                "cooling",
            )

            assert cycle.in_min_runtime_hold is True
            stored = await db.get_cycle_log(conn, cycle.id)
            assert stored is not None and stored.in_min_runtime_hold is True
            assert "r2" in engine._room_cycle_states, "the room still joined the cycle"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_room_joining_with_demand_releases_the_hold(self):
        """Control: the same join, but 80°F against the same 72°F target."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_cycle_runtime_min=10))
            held = await _add_room(conn, "r1", "Study", vents=["cover.r1"])
            joiner = await _add_room(conn, "r2", "Den", vents=["cover.r2"])
            ha = _make_ha(ambient=78.0, sensors={"sensor.r1": 71.0, "sensor.r2": 80.0})
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(held, target=72.0)})
            cycle.in_min_runtime_hold = True
            await db.set_cycle_log_min_runtime_hold(conn, cycle.id, True)
            engine._sensor_map = {"r1": ["sensor.r1"], "r2": ["sensor.r2"]}

            await engine._start_or_update_cycle(
                conn,
                {"r1": _ar(held, target=72.0), "r2": _ar(joiner, target=72.0)},
                "cooling",
            )

            assert cycle.in_min_runtime_hold is False
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _start_or_update_cycle: the active-vent reopen sweep skips served rooms
# (1227->1225)
# ---------------------------------------------------------------------------


class TestActiveVentSweepSkips:
    @pytest.mark.asyncio
    async def test_served_and_stateless_rooms_are_not_reopened(self):
        """The end-of-update sweep re-opens active rooms' vents. A room that
        already reached target (``vent_closed_at`` set) must NOT be reopened —
        that would undo the close and restart the room's conditioning — and a
        room with no cycle state at all is left to the #427 repair path."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            served = await _add_room(conn, "r1", "Study", vents=["cover.r1"])
            stateless = await _add_room(conn, "r2", "Den", vents=["cover.r2"])
            ha = _make_ha(ambient=78.0, sensors={"sensor.r1": 70.0, "sensor.r2": 70.0})
            rooms = {"r1": _ar(served), "r2": _ar(stateless)}
            engine, cycle = await _running_engine(ha, conn, rooms)
            engine._room_cycle_states["r1"].vent_closed_at = datetime.now(UTC)
            engine._room_cycle_states.pop("r2")

            await engine._start_or_update_cycle(conn, rooms, "cooling")

            ha.open_cover.assert_not_awaited()
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _monitor_rooms / _terminate_cycle without a cycle log
# (1561->1576, 1755->1769, 1769->1790)
# ---------------------------------------------------------------------------


class TestMonitorAndTerminateWithoutCycleLog:
    @pytest.mark.asyncio
    async def test_room_is_served_and_cycle_ends_with_no_cycle_log(self):
        """A RUNNING engine whose ``_cycle_log`` is None (the restore edge case
        the guards exist for) must still close the vent of a room that reached
        target and still terminate the cycle — it simply writes no
        diagnostics."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            room = await _add_room(conn, "r1", "Study", vents=["cover.r1"])
            # 70°F against a 72°F cooling target — at target.
            ha = _make_ha(ambient=78.0, cover_state="open", sensors={"sensor.r1": 70.0})
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(room, target=72.0)})
            rcs = engine._room_cycle_states["r1"]
            # The in-memory handle is gone (a restore that never rehydrated it)
            # while the row itself still exists.
            engine._cycle_log = None

            await engine._monitor_rooms(conn, "cooling")

            assert rcs.vent_closed_at is not None, "the room was served"
            assert rcs.temp_at_end == 70.0
            assert await _vent_event_count(conn) == 0, "no cycle log → no vent diagnostics"
            assert engine.cycle_state == CycleState.IDLE, "the cycle still terminated"
            assert engine._last_cycle_ended_at is not None, "the off-time lockout is armed"
            assert engine._active_rooms == {}
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_terminating_without_a_cycle_log_writes_no_diagnostics(self, caplog):
        """``_terminate_cycle`` guards both of its diagnostics blocks on the
        cycle log. With none in hand it must skip the per-room ``temp_at_end``
        backfill and the log close outright — quietly, not as an error — while
        still parking the setpoint, reopening the zone and arming the off-time
        lockout."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            room = await _add_room(conn, "r1", "Study", vents=["cover.r1"])
            ha = _make_ha(ambient=78.0, cover_state="closed", sensors={"sensor.r1": 70.0})
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(room, target=72.0)})
            rcs = engine._room_cycle_states["r1"]
            assert rcs.temp_at_end is None
            engine._cycle_log = None

            with caplog.at_level(logging.ERROR, logger="backend.engine.cycle_engine"):
                await engine._terminate_cycle(conn, reason="completed")

            assert rcs.temp_at_end is None, "no cycle log → no per-room end temperature is recorded"
            stored = await db.get_cycle_log(conn, cycle.id)
            assert stored is not None and stored.ended_at is None, (
                "the row the engine has no handle on is left untouched"
            )
            assert not any("Failed to close cycle log" in r.message for r in caplog.records), (
                "skipping the close is the designed path, not an error"
            )
            # ambient 78 + overshoot 2, parked to the cooling idle side.
            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 80.0, hvac_mode="cool"
            )
            assert engine.cycle_state == CycleState.IDLE
            assert engine._last_cycle_ended_at is not None
            opened = [c.args[0] for c in ha.open_cover.await_args_list]
            assert "cover.r1" in opened
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _reopen_drifted_room: no vents, no cycle log (1643->1645, 1657->1672)
# ---------------------------------------------------------------------------


class TestReopenDriftedRoomDegraded:
    @pytest.mark.asyncio
    async def test_ventless_room_is_re_engaged_without_cover_traffic_or_diagnostics(self, caplog):
        """A sensor-only room can still be "served" and then drift back past
        its deadband. There is no vent to reopen and (here) no cycle log to
        record against, but the room must be re-engaged in the cycle:
        ``vent_closed_at`` cleared so the monitor keeps serving it, and
        ``temp_at_end`` cleared so it is no longer treated as finished."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            room = await _add_room(conn, "r1", "Hall")  # no vents
            ha = _make_ha(ambient=78.0, sensors={"sensor.r1": 76.0})
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(room, target=72.0)})
            rcs = engine._room_cycle_states["r1"]
            rcs.vent_closed_at = datetime.now(UTC)
            rcs.reached_at = datetime.now(UTC) - timedelta(minutes=5)
            rcs.temp_at_end = 71.0
            engine._cycle_log = None
            reached_before = rcs.reached_at

            with caplog.at_level(logging.INFO, logger="backend.engine.cycle_engine"):
                await engine._reopen_drifted_room(
                    conn, "r1", _ar(room, target=72.0), rcs, 76.0, 0.5
                )

            assert rcs.vent_closed_at is None
            assert rcs.temp_at_end is None
            assert rcs.reached_at == reached_before, "time-to-target metrics are preserved"
            ha.open_cover.assert_not_awaited()
            assert await _vent_event_count(conn) == 0
            assert any("vent reopened" in r.message for r in caplog.records)
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _terminate_cycle: unreadable thermostat (1791->1834, 1796->1834)
# ---------------------------------------------------------------------------


class TestTerminateWithUnreadableThermostat:
    @pytest.mark.asyncio
    async def test_missing_thermostat_state_skips_setpoint_parking(self):
        """The climate entity vanished between the last tick and termination:
        there is no ambient to park against, so no setpoint is commanded —
        but the cycle must still close out and the zone vents must reopen."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            room = await _add_room(conn, "r1", "Study", vents=["cover.r1"])
            ha = _make_ha(
                thermostat_missing=True, cover_state="closed", sensors={"sensor.r1": 70.0}
            )
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(room)})

            await engine._terminate_cycle(conn, reason="completed")

            ha.set_thermostat_temperature.assert_not_awaited()
            assert engine._last_setpoint_sent is None
            assert engine.cycle_state == CycleState.IDLE
            stored = await db.get_cycle_log(conn, cycle.id)
            assert stored is not None and stored.ended_reason == "completed"
            opened = [c.args[0] for c in ha.open_cover.await_args_list]
            assert "cover.r1" in opened, "the zone returns to a fully open idle state"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_thermostat_without_an_ambient_reading_skips_setpoint_parking(self):
        """The entity answers but its ``current_temperature`` is missing (a
        booting integration). Parking at an unknown ambient would command a
        garbage setpoint, so the engine leaves the thermostat alone."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            room = await _add_room(conn, "r1", "Study", vents=["cover.r1"])
            ha = _make_ha(ambient=None, setpoint=74.0, sensors={"sensor.r1": 70.0})
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(room)})

            await engine._terminate_cycle(conn, reason="completed")

            ha.set_thermostat_temperature.assert_not_awaited()
            assert engine.cycle_state == CycleState.IDLE
            stored = await db.get_cycle_log(conn, cycle.id)
            assert stored is not None and stored.ended_at is not None
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _suppression_vote: no thermostat config → no hard cap (2193->2199)
# ---------------------------------------------------------------------------


class TestSuppressionVoteWithoutConfig:
    @staticmethod
    def _room() -> Room:
        return Room.create(
            name="Sunroom",
            thermostat_entity_id=THERMO_ID,
            ambient_suppression_enabled=True,
            ambient_suppression_mode="any_presence",
            ambient_suppression_min_differential=5.0,
            ambient_suppression_deadband=2.0,
        )

    def test_without_a_config_the_comfort_hard_cap_does_not_apply(self):
        """``tc`` is optional on this helper. With no config there is no
        min/max envelope to enforce, so a coasting room keeps coasting."""
        ha = _make_ha()
        engine = _make_engine(ha)

        vote, suppressed = engine._suppression_vote(
            self._room(),
            effective=70.5,
            target=72.0,
            source="presence",
            normal_deadband=0.5,
            outside_temp=80.0,
            tc=None,
        )

        assert (vote, suppressed) == ("off", True), (
            "outside air is warm enough to carry the room up — no HVAC called for"
        )

    def test_with_a_config_the_hard_cap_overrides_the_coast(self):
        """Control: identical room and temperatures, but a 71°F comfort floor.
        70.5°F is below it, so suppression is overridden and heat is called."""
        ha = _make_ha()
        engine = _make_engine(ha)

        vote, suppressed = engine._suppression_vote(
            self._room(),
            effective=70.5,
            target=72.0,
            source="presence",
            normal_deadband=0.5,
            outside_temp=80.0,
            tc=_tc(min_setpoint=71.0),
        )

        assert (vote, suppressed) == ("heat", False)


# ---------------------------------------------------------------------------
# _infer_mode_from_room_temps: no thermostat ambient → no sanity check
# (2283->2321)
# ---------------------------------------------------------------------------


class TestInferModeWithoutThermostatAmbient:
    @staticmethod
    def _engine_with_cold_room() -> tuple[CycleEngine, dict[str, ActiveRoom]]:
        ha = _make_ha(sensors={"sensor.r1": 60.0})
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["sensor.r1"]}
        room = Room.create(name="Study", thermostat_entity_id=THERMO_ID)
        room.id = "r1"
        return engine, {"r1": _ar(room, target=72.0)}

    @pytest.mark.asyncio
    async def test_no_thermostat_state_leaves_the_room_vote_intact(self, caplog):
        engine, rooms = self._engine_with_cold_room()

        with caplog.at_level(logging.WARNING, logger="backend.engine.cycle_engine"):
            mode = await engine._infer_mode_from_room_temps(rooms, 0.5, thermo_state=None)

        assert mode == "heating"
        assert not any("Mode contradiction" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_unreadable_ambient_leaves_the_room_vote_intact(self, caplog):
        """The thermostat answers but reports no temperature — there is
        nothing to cross-validate against, so the room vote stands."""
        engine, rooms = self._engine_with_cold_room()

        with caplog.at_level(logging.WARNING, logger="backend.engine.cycle_engine"):
            mode = await engine._infer_mode_from_room_temps(
                rooms, 0.5, thermo_state={"state": "off", "attributes": {}}
            )

        assert mode == "heating"
        assert not any("Mode contradiction" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_readable_contradicting_ambient_flips_the_vote(self, caplog):
        """Control: with a real 90°F ambient the #38 sanity check fires."""
        engine, rooms = self._engine_with_cold_room()

        with caplog.at_level(logging.WARNING, logger="backend.engine.cycle_engine"):
            mode = await engine._infer_mode_from_room_temps(
                rooms,
                0.5,
                thermo_state={"state": "cool", "attributes": {"current_temperature": 90.0}},
            )

        assert mode == "cooling"
        assert any("Mode contradiction" in r.message for r in caplog.records)
