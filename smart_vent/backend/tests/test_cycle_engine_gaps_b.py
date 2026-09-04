"""Coverage gaps in ``backend/engine/cycle_engine.py`` (lines ~1600-3200).

Focus: the defensive/contained-failure paths around cycle termination and
abort, the #427 missing-room-state self-repair, the ambient mode-contradiction
override, and the reconcile re-assert failure path.  Every test drives the real
engine method and asserts observable state (DB rows, HA service calls, engine
state) rather than only that "nothing raised".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from backend import db
from backend.engine import cycle_engine as ce_mod
from backend.engine.cycle_engine import CycleEngine, CycleState
from backend.engine.room_manager import ActiveRoom
from backend.engine.vent_controller import VentController
from backend.models import (
    CycleLog,
    Room,
    RoomCycleState,
    RoomSensor,
    RoomVent,
    ThermostatConfig,
)

THERMO_ID = "climate.test_thermostat"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_ha(ambient: float = 70.0, setpoint: float = 68.0, vent_state: str = "closed"):
    ha = MagicMock()
    ha.ha_temp_unit = "F"

    def _get_state(eid):
        if eid == THERMO_ID:
            return {
                "state": "cool",
                "attributes": {
                    "current_temperature": ambient,
                    "temperature": setpoint,
                    "hvac_action": "cooling",
                },
            }
        return {"state": vent_state, "attributes": {}}

    ha.get_state.side_effect = _get_state
    ha.get_numeric_state.return_value = None
    ha.set_thermostat_temperature = AsyncMock()
    ha.open_cover = AsyncMock()
    ha.close_cover = AsyncMock()
    ha.set_cover_position = AsyncMock()
    ha.set_cover_tilt_position = AsyncMock()
    ha.toggle_cover = AsyncMock()
    return ha


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict | None]] = []

    async def log(self, level, category, message, details=None):
        self.events.append((level, category, message, details))


def _make_engine(ha=None, logger=None) -> CycleEngine:
    ha = ha or _make_ha()
    engine = CycleEngine(
        thermostat_entity_id=THERMO_ID,
        ha=ha,
        vent_ctrl=VentController(ha),
        get_enabled=lambda: True,
    )
    if logger is not None:
        engine._logger = logger
    return engine


async def _fresh_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    await db.upsert_thermostat_config(conn, ThermostatConfig(thermostat_entity_id=THERMO_ID))
    return conn


async def _add_room(conn, room_id: str, name: str, *, vent: bool = True, sensor: bool = True):
    room = Room(id=room_id, name=name, thermostat_entity_id=THERMO_ID)
    await db.upsert_room(conn, room)
    if sensor:
        await db.add_room_sensor(
            conn, RoomSensor.create(room_id=room_id, entity_id=f"sensor.{room_id}_temp")
        )
    if vent:
        await db.add_room_vent(conn, RoomVent.create(room_id, f"cover.{room_id}_vent"))
    return room


async def _running_cycle(engine: CycleEngine, conn, room: Room, target: float = 72.0):
    """Put *engine* into a RUNNING cooling cycle serving *room*."""
    vents = await db.get_room_vents(conn, room.id)
    cycle = CycleLog.create(
        thermostat_entity_id=THERMO_ID,
        mode="cooling",
        rooms_json=json.dumps({room.id: {"name": room.name, "target": target}}),
    )
    await db.insert_cycle_log(conn, cycle)
    rcs = RoomCycleState(cycle_id=cycle.id, room_id=room.id, target_temp=target)
    await db.upsert_room_cycle_state(conn, rcs)

    engine._state = CycleState.RUNNING
    engine._cycle_log = cycle
    engine._cycle_mode = "cooling"
    engine._cycle_ha_mode = "cool"
    engine._active_rooms = {room.id: ActiveRoom(room=room, target_temp=target, source="schedule")}
    engine._room_cycle_states = {room.id: rcs}
    engine._room_vents = {room.id: vents}
    return cycle, rcs


# ---------------------------------------------------------------------------
# _reopen_drifted_room — diagnostics insert failure must not block the reopen
# ---------------------------------------------------------------------------


class TestReopenDriftedRoomEventFailure:
    @pytest.mark.asyncio
    async def test_vent_event_insert_failure_still_reopens_the_vent(self, monkeypatch):
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            engine = _make_engine(_make_ha(vent_state="closed"))
            await engine.load_room_sensors(conn, [room.id])
            _cycle, rcs = await _running_cycle(engine, conn, room, target=72.0)

            # Room is "served" (vent already closed) but has drifted a full
            # deadband past target, so _monitor_rooms must reopen it.
            rcs.vent_closed_at = datetime.now(UTC) - timedelta(minutes=2)
            rcs.temp_at_end = 72.0
            await db.upsert_room_cycle_state(conn, rcs)
            engine._ha.get_numeric_state.return_value = 80.0

            real_insert = db.insert_cycle_vent_event

            async def _boom(c, cycle_id, ts, entity_id, room_id, action, detail=None):
                if action == "reopened_drift":
                    raise RuntimeError("diagnostics table locked")
                return await real_insert(c, cycle_id, ts, entity_id, room_id, action, detail)

            monkeypatch.setattr(ce_mod.db, "insert_cycle_vent_event", _boom)

            await engine._monitor_rooms(conn, "cooling")

            # The reopen itself survived the diagnostics failure.
            engine._ha.open_cover.assert_awaited_with("cover.r1_vent")
            assert rcs.vent_closed_at is None
            assert rcs.temp_at_end is None
            stored = await db.get_room_cycle_states(conn, _cycle.id)
            assert stored[0].vent_closed_at is None
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _repair_missing_room_state (#427)
# ---------------------------------------------------------------------------


class TestRepairMissingRoomState:
    @pytest.mark.asyncio
    async def test_no_cycle_log_means_no_repair_and_no_termination(self):
        """With no cycle log there is nothing to attach the room to — the
        repair bails out and the cycle is NOT allowed to terminate."""
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            engine = _make_engine()
            await engine.load_room_sensors(conn, [room.id])
            engine._state = CycleState.RUNNING
            engine._cycle_mode = "cooling"
            engine._cycle_ha_mode = "cool"
            engine._cycle_log = None
            engine._active_rooms = {
                room.id: ActiveRoom(room=room, target_temp=72.0, source="schedule")
            }
            engine._room_cycle_states = {}
            engine._room_vents = {room.id: await db.get_room_vents(conn, room.id)}

            await engine._monitor_rooms(conn, "cooling")

            assert engine._room_cycle_states == {}
            assert engine.cycle_state == CycleState.RUNNING  # never terminated
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_repair_loads_vents_from_db_when_not_cached(self):
        """A room whose vents were never cached (the interrupted-join case)
        gets them loaded from the DB and opened."""
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            ha = _make_ha(vent_state="closed")
            logger = _RecordingLogger()
            engine = _make_engine(ha, logger)
            await engine.load_room_sensors(conn, [room.id])
            cycle = CycleLog.create(thermostat_entity_id=THERMO_ID, mode="cooling", rooms_json="{}")
            await db.insert_cycle_log(conn, cycle)
            engine._state = CycleState.RUNNING
            engine._cycle_log = cycle
            engine._cycle_mode = "cooling"
            engine._cycle_ha_mode = "cool"
            engine._active_rooms = {
                room.id: ActiveRoom(room=room, target_temp=72.0, source="schedule")
            }
            engine._room_cycle_states = {}
            engine._room_vents = {}  # nothing cached — must come from the DB
            ha.get_numeric_state.return_value = 80.0

            await engine._monitor_rooms(conn, "cooling")

            assert [v.entity_id for v in engine._room_vents[room.id]] == ["cover.r1_vent"]
            ha.open_cover.assert_any_await("cover.r1_vent")
            repaired = engine._room_cycle_states[room.id]
            assert repaired.cycle_id == cycle.id
            assert repaired.target_temp == 72.0
            assert repaired.joined_at is not None
            persisted = await db.get_room_cycle_states(conn, cycle.id)
            assert [r.room_id for r in persisted] == [room.id]
            assert any("Repaired missing cycle state" in e[2] for e in logger.events)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_repair_failure_returns_none_and_blocks_termination(self, monkeypatch):
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            engine = _make_engine()
            await engine.load_room_sensors(conn, [room.id])
            cycle = CycleLog.create(thermostat_entity_id=THERMO_ID, mode="cooling", rooms_json="{}")
            await db.insert_cycle_log(conn, cycle)
            engine._state = CycleState.RUNNING
            engine._cycle_log = cycle
            engine._cycle_mode = "cooling"
            engine._cycle_ha_mode = "cool"
            engine._active_rooms = {
                room.id: ActiveRoom(room=room, target_temp=72.0, source="schedule")
            }
            engine._room_cycle_states = {}
            engine._room_vents = {room.id: await db.get_room_vents(conn, room.id)}

            async def _boom(*_a, **_kw):
                raise RuntimeError("database is locked")

            monkeypatch.setattr(ce_mod.db, "upsert_room_cycle_state", _boom)

            await engine._monitor_rooms(conn, "cooling")

            # Nothing was persisted and the room's vents were never opened, so
            # the repair reported failure and termination stays blocked.
            assert await db.get_room_cycle_states(conn, cycle.id) == []
            engine._ha.open_cover.assert_not_awaited()
            assert engine.cycle_state == CycleState.RUNNING
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _terminate_cycle — contained failures
# ---------------------------------------------------------------------------


class TestTerminateContainedFailures:
    @pytest.mark.asyncio
    async def test_room_state_persist_failure_does_not_stop_termination(self, monkeypatch):
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            engine = _make_engine()
            await engine.load_room_sensors(conn, [room.id])
            cycle, _rcs = await _running_cycle(engine, conn, room)
            engine._ha.get_numeric_state.return_value = 74.0

            async def _boom(*_a, **_kw):
                raise RuntimeError("database is locked")

            monkeypatch.setattr(ce_mod.db, "upsert_room_cycle_state", _boom)

            await engine._terminate_cycle(conn)

            assert engine.cycle_state == CycleState.IDLE
            closed = await db.get_cycle_log(conn, cycle.id)
            assert closed.ended_reason == "completed"
            assert closed.ended_at is not None
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_close_cycle_log_failure_still_parks_and_reopens(self, monkeypatch):
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            ha = _make_ha(vent_state="closed")
            engine = _make_engine(ha)
            await engine.load_room_sensors(conn, [room.id])
            cycle, _rcs = await _running_cycle(engine, conn, room)

            async def _boom(*_a, **_kw):
                raise RuntimeError("database is locked")

            monkeypatch.setattr(ce_mod.db, "close_cycle_log", _boom)

            await engine._terminate_cycle(conn)

            # DB row stays open (the write failed) but the engine still went
            # idle, parked the thermostat and re-opened the zone.
            still_open = await db.get_cycle_log(conn, cycle.id)
            assert still_open.ended_at is None
            assert engine.cycle_state == CycleState.IDLE
            ha.set_thermostat_temperature.assert_awaited()
            ha.open_cover.assert_awaited_with("cover.r1_vent")
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_idle_room_vent_lookup_failure_is_contained(self, monkeypatch):
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            ha = _make_ha(vent_state="closed")
            engine = _make_engine(ha)
            await engine.load_room_sensors(conn, [room.id])
            cycle, _rcs = await _running_cycle(engine, conn, room)

            async def _boom(*_a, **_kw):
                raise RuntimeError("database is locked")

            monkeypatch.setattr(ce_mod.db, "get_rooms_for_thermostat", _boom)

            await engine._terminate_cycle(conn)

            assert engine.cycle_state == CycleState.IDLE
            closed = await db.get_cycle_log(conn, cycle.id)
            assert closed.ended_reason == "completed"
            # The active room's own vents are still re-opened.
            ha.open_cover.assert_awaited_with("cover.r1_vent")
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_vent_reopen_failure_is_contained(self):
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            ha = _make_ha(vent_state="closed")
            logger = _RecordingLogger()
            engine = _make_engine(ha, logger)
            await engine.load_room_sensors(conn, [room.id])
            cycle, _rcs = await _running_cycle(engine, conn, room)
            engine._vent.open_room_vents = AsyncMock(side_effect=RuntimeError("HA offline"))

            await engine._terminate_cycle(conn)

            engine._vent.open_room_vents.assert_awaited()
            assert engine.cycle_state == CycleState.IDLE
            closed = await db.get_cycle_log(conn, cycle.id)
            assert closed.ended_reason == "completed"
            # The "all zone vents re-opened" success event was never emitted.
            assert not any("all zone vents re-opened" in e[2] for e in logger.events)
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _abort_cycle — contained failures + idle-room vents
# ---------------------------------------------------------------------------


class TestAbortContainedFailures:
    @pytest.mark.asyncio
    async def test_room_state_persist_failure_does_not_stop_abort(self, monkeypatch):
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            engine = _make_engine()
            await engine.load_room_sensors(conn, [room.id])
            cycle, _rcs = await _running_cycle(engine, conn, room)
            engine._ha.get_numeric_state.return_value = 74.0

            async def _boom(*_a, **_kw):
                raise RuntimeError("database is locked")

            monkeypatch.setattr(ce_mod.db, "upsert_room_cycle_state", _boom)

            await engine._abort_cycle(conn, reason="system disabled")

            assert engine.cycle_state == CycleState.IDLE
            closed = await db.get_cycle_log(conn, cycle.id)
            assert closed.ended_reason == "aborted: system disabled"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_close_cycle_log_failure_still_reopens_and_parks(self, monkeypatch):
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            ha = _make_ha(vent_state="closed")
            engine = _make_engine(ha)
            await engine.load_room_sensors(conn, [room.id])
            cycle, _rcs = await _running_cycle(engine, conn, room)

            async def _boom(*_a, **_kw):
                raise RuntimeError("database is locked")

            monkeypatch.setattr(ce_mod.db, "close_cycle_log", _boom)

            await engine._abort_cycle(conn, reason="hvac unavailable")

            still_open = await db.get_cycle_log(conn, cycle.id)
            assert still_open.ended_at is None
            assert engine.cycle_state == CycleState.IDLE
            ha.open_cover.assert_awaited_with("cover.r1_vent")
            ha.set_thermostat_temperature.assert_awaited()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_abort_reopens_idle_room_vents_too(self):
        """Issue #244: rooms closed at cycle start but not part of the cycle
        must also be re-opened when the cycle aborts."""
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            await _add_room(conn, "r2", "Spare Room")  # idle, never joined
            ha = _make_ha(vent_state="closed")
            engine = _make_engine(ha)
            await engine.load_room_sensors(conn, [room.id])
            await _running_cycle(engine, conn, room)

            await engine._abort_cycle(conn, reason="system disabled")

            opened = {c.args[0] for c in ha.open_cover.await_args_list}
            assert opened == {"cover.r1_vent", "cover.r2_vent"}
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_idle_room_vent_lookup_failure_is_contained(self, monkeypatch):
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            await _add_room(conn, "r2", "Spare Room")
            ha = _make_ha(vent_state="closed")
            engine = _make_engine(ha)
            await engine.load_room_sensors(conn, [room.id])
            cycle, _rcs = await _running_cycle(engine, conn, room)

            async def _boom(*_a, **_kw):
                raise RuntimeError("database is locked")

            monkeypatch.setattr(ce_mod.db, "get_rooms_for_thermostat", _boom)

            await engine._abort_cycle(conn, reason="system disabled")

            assert engine.cycle_state == CycleState.IDLE
            closed = await db.get_cycle_log(conn, cycle.id)
            assert closed.ended_reason == "aborted: system disabled"
            # Only the active room's vents could be recovered.
            opened = {c.args[0] for c in ha.open_cover.await_args_list}
            assert opened == {"cover.r1_vent"}
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_vent_reopen_failure_is_contained(self):
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            ha = _make_ha(vent_state="closed")
            logger = _RecordingLogger()
            engine = _make_engine(ha, logger)
            await engine.load_room_sensors(conn, [room.id])
            cycle, _rcs = await _running_cycle(engine, conn, room)
            engine._vent.open_room_vents = AsyncMock(side_effect=RuntimeError("HA offline"))

            await engine._abort_cycle(conn, reason="system disabled")

            engine._vent.open_room_vents.assert_awaited()
            assert engine.cycle_state == CycleState.IDLE
            closed = await db.get_cycle_log(conn, cycle.id)
            assert closed.ended_reason == "aborted: system disabled"
            assert not any("all zone vents re-opened" in e[2] for e in logger.events)
            # Parking still happens after the failed vent work.
            ha.set_thermostat_temperature.assert_awaited()
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _on_presence
# ---------------------------------------------------------------------------


class TestOnPresence:
    @pytest.mark.asyncio
    async def test_presence_during_running_cycle_creates_holdover(self):
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            engine = _make_engine()
            engine._state = CycleState.RUNNING

            await engine._on_presence(conn, room)

            holdover = await db.get_holdover_state(conn, room.id)
            assert holdover is not None
            assert holdover.expires_at > datetime.now(UTC)
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _infer_mode_from_room_temps
# ---------------------------------------------------------------------------


class TestInferModeEdges:
    @pytest.mark.asyncio
    async def test_room_with_no_reading_and_no_thermostat_ambient_is_skipped(self):
        """No sensor data and no thermostat proxy → the room casts no vote."""
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            engine = _make_engine()
            await engine.load_room_sensors(conn, [room.id])
            engine._ha.get_numeric_state.return_value = None  # sensor unreadable

            mode = await engine._infer_mode_from_room_temps(
                {room.id: ActiveRoom(room=room, target_temp=72.0, source="schedule")},
                deadband=1.0,
                thermo_state=None,  # no thermostat ambient either
            )

            assert mode == "off"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_ambient_far_below_targets_flips_cooling_vote_to_heating(self):
        """Rooms vote 'cooling' but the thermostat reads far colder than every
        target — the ambient sanity check overrides and warns (#38)."""
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            logger = _RecordingLogger()
            engine = _make_engine(logger=logger)
            await engine.load_room_sensors(conn, [room.id])
            engine._ha.get_numeric_state.return_value = 80.0  # room says "cool me"

            mode = await engine._infer_mode_from_room_temps(
                {room.id: ActiveRoom(room=room, target_temp=70.0, source="schedule")},
                deadband=1.0,
                thermo_state={"attributes": {"current_temperature": 50.0}},
            )

            assert mode == "heating"
            warnings = [e for e in logger.events if e[0] == "warning"]
            assert len(warnings) == 1
            _level, category, message, details = warnings[0]
            assert category == "engine"
            assert "Mode contradiction" in message
            assert details["room_vote"] == "cooling"
            assert details["corrected_mode"] == "heating"
            assert details["thermostat_ambient"] == pytest.approx(50.0)
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _filter_rooms_for_mode
# ---------------------------------------------------------------------------


class TestFilterRoomsForMode:
    @pytest.mark.asyncio
    async def test_room_needing_heat_is_dropped_from_a_cooling_cycle_and_logged(self):
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            logger = _RecordingLogger()
            engine = _make_engine(logger=logger)
            await engine.load_room_sensors(conn, [room.id])
            engine._ha.get_numeric_state.return_value = 60.0  # well below target

            filtered = await engine._filter_rooms_for_mode(
                {room.id: ActiveRoom(room=room, target_temp=72.0, source="schedule")},
                "cooling",
                1.0,
                None,
            )

            assert filtered == {}
            infos = [e for e in logger.events if e[0] == "info"]
            assert len(infos) == 1
            _level, category, message, details = infos[0]
            assert category == "engine"
            assert "Excluding room Bedroom from cooling cycle" in message
            assert details["room_id"] == room.id
            assert details["cycle_mode"] == "cooling"
            assert details["effective_temp"] == pytest.approx(60.0)
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _read_outside_temp
# ---------------------------------------------------------------------------


class TestReadOutsideTemp:
    @pytest.mark.asyncio
    async def test_ha_read_error_yields_none_rather_than_propagating(self):
        conn = await _fresh_db()
        try:
            await db.set_system_setting(conn, "outside_temperature_entity_id", "sensor.outside")
            engine = _make_engine()
            engine._ha.get_numeric_state.side_effect = RuntimeError("websocket closed")

            assert await engine._read_outside_temp(conn) is None
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_reads_the_configured_entity_when_healthy(self):
        conn = await _fresh_db()
        try:
            await db.set_system_setting(conn, "outside_temperature_entity_id", "sensor.outside")
            engine = _make_engine()
            engine._ha.get_numeric_state.return_value = 91.5

            assert await engine._read_outside_temp(conn) == pytest.approx(91.5)
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _set_thermostat_setpoint — ambient-anchor arithmetic guard
# ---------------------------------------------------------------------------


class _IntOnlyDelta:
    """A stand-in overshoot_delta that only knows how to subtract from ints.

    Models the defensive case the ``except (ValueError, TypeError)`` around the
    ambient anchor exists for: the target-derived arithmetic succeeds, but the
    ambient-derived arithmetic (a float) raises.
    """

    def __rsub__(self, other):
        if isinstance(other, int):
            return other - 2
        raise TypeError("unsupported operand")

    def __radd__(self, other):
        if isinstance(other, int):
            return other + 2
        raise TypeError("unsupported operand")

    def __format__(self, spec):  # pragma: no cover - only used by log formatting
        return "int-only"


class TestAmbientAnchorArithmeticGuard:
    @pytest.mark.asyncio
    async def test_failed_ambient_anchor_leaves_the_target_derived_setpoint(self):
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            ha = _make_ha(ambient=70.0)
            engine = _make_engine(ha)
            await engine.load_room_sensors(conn, [room.id])
            # int target so `min(targets) - delta` works, ambient is a float so
            # the anchor arithmetic raises TypeError and is swallowed.
            engine._active_rooms = {
                room.id: ActiveRoom(room=room, target_temp=72, source="schedule")
            }
            tc = ThermostatConfig(thermostat_entity_id=THERMO_ID)
            tc.overshoot_delta = _IntOnlyDelta()

            await engine._set_thermostat_setpoint(tc, "cooling")

            # 72 - 2 = 70; the ambient clamp (which would have produced 68)
            # never ran because its arithmetic raised.
            ha.set_thermostat_temperature.assert_awaited_once_with(THERMO_ID, 70, hvac_mode="cool")
            assert engine._last_setpoint_sent == 70
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _reconcile_state — re-assert failure
# ---------------------------------------------------------------------------


class TestReconcileReassertFailure:
    @pytest.mark.asyncio
    async def test_reassert_service_failure_is_logged_not_raised(self):
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            # Thermostat drifted: mode 'off' and setpoint 75 vs our 68.
            ha = _make_ha(vent_state="open")
            ha.get_state.side_effect = lambda eid: (
                {
                    "state": "off",
                    "attributes": {"current_temperature": 70.0, "temperature": 75.0},
                }
                if eid == THERMO_ID
                else {"state": "open", "attributes": {}}
            )
            ha.set_thermostat_temperature = AsyncMock(side_effect=RuntimeError("HA offline"))
            logger = _RecordingLogger()
            engine = _make_engine(ha, logger)
            await engine.load_room_sensors(conn, [room.id])
            await _running_cycle(engine, conn, room)
            engine._last_setpoint_sent = 68.0

            tc = await db.get_thermostat_config(conn, THERMO_ID)
            await engine._reconcile_state(conn, tc)

            # Both drifts were detected and a re-assert was attempted...
            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 68.0, hvac_mode="cool"
            )
            reconcile_warnings = [
                e for e in logger.events if e[0] == "warning" and e[1] == "reconcile"
            ]
            assert {
                d["actual_mode"] for _l, _c, _m, d in reconcile_warnings if "actual_mode" in d
            } == {"off"}
            assert any(
                d.get("actual") == pytest.approx(75.0) for _l, _c, _m, d in reconcile_warnings if d
            )
            # ...and the failure did not abort the cycle.
            assert engine.cycle_state == CycleState.RUNNING
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _set_thermostat_setpoint — diagnostics + HA service failures
# ---------------------------------------------------------------------------


class TestSetpointCommandFailures:
    @pytest.mark.asyncio
    async def test_setpoint_history_write_failure_does_not_lose_the_command(self, monkeypatch):
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            ha = _make_ha(ambient=78.0)
            logger = _RecordingLogger()
            engine = _make_engine(ha, logger)
            await engine.load_room_sensors(conn, [room.id])
            cycle, _rcs = await _running_cycle(engine, conn, room, target=72.0)

            async def _boom(*_a, **_kw):
                raise RuntimeError("database is locked")

            monkeypatch.setattr(ce_mod.db, "insert_cycle_setpoint_history", _boom)

            tc = ThermostatConfig(thermostat_entity_id=THERMO_ID, overshoot_delta=2.0)
            await engine._set_thermostat_setpoint(tc, "cooling", conn=conn)

            # The thermostat was still commanded and the engine still tracks it.
            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 70.0, hvac_mode="cool"
            )
            assert engine._last_setpoint_sent == pytest.approx(70.0)
            # No history row survived the failed write, but the success event
            # log still fired.
            assert await db.get_cycle_setpoint_history(conn, cycle.id) == []
            assert any("Setpoint for" in e[2] for e in logger.events)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_ha_service_failure_leaves_last_setpoint_untracked(self):
        """If the HA call fails there is nothing to reconcile against, so
        ``_last_setpoint_sent`` must not be advanced and no history is written."""
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "r1", "Bedroom")
            ha = _make_ha(ambient=78.0)
            ha.set_thermostat_temperature = AsyncMock(side_effect=RuntimeError("HA offline"))
            logger = _RecordingLogger()
            engine = _make_engine(ha, logger)
            await engine.load_room_sensors(conn, [room.id])
            cycle, _rcs = await _running_cycle(engine, conn, room, target=72.0)
            engine._last_setpoint_sent = None

            tc = ThermostatConfig(thermostat_entity_id=THERMO_ID, overshoot_delta=2.0)
            await engine._set_thermostat_setpoint(tc, "cooling", conn=conn)

            ha.set_thermostat_temperature.assert_awaited_once()
            assert engine._last_setpoint_sent is None
            assert await db.get_cycle_setpoint_history(conn, cycle.id) == []
            assert not any("Setpoint for" in e[2] for e in logger.events)
        finally:
            await conn.close()
