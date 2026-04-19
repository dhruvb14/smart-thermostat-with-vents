"""
Tests for cycle diagnostics (Issue #60).

Covers:
  - Cycle start captures thermostat_temp_at_start, setpoint_at_start, vents_at_start
  - Per-room temp_at_start, trigger_detail, joined_at
  - Cycle termination: ended_reason="completed", end-state snapshots
  - Cycle abort: ended_reason="aborted: <reason>" prefix
  - Cycle timeout: ended_reason="timeout"
  - Temp samples written per tick to cycle_temp_samples
  - Setpoint history written on each setpoint change
  - Vent events recorded at cycle start (opened_at_start), on target reach
    (closed_reached_target), and on safety force-reopen (force_reopened_max_closed)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
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
    RoomVent,
    Schedule,
    ThermostatConfig,
)

THERMO_ID = "climate.test_thermostat"


def _make_ha(ambient: float = 72.0, setpoint: float = 75.0) -> MagicMock:
    ha = MagicMock()
    ha.get_state.return_value = {
        "state": "cool",
        "attributes": {
            "current_temperature": ambient,
            "temperature": setpoint,
            "hvac_action": "cooling",
        },
    }
    ha.get_numeric_state.return_value = None
    ha.set_thermostat_temperature = AsyncMock()
    ha.open_cover = AsyncMock()
    ha.close_cover = AsyncMock()
    ha.set_cover_position = AsyncMock()
    ha.set_cover_tilt_position = AsyncMock()
    ha.toggle_cover = AsyncMock()
    return ha


def _make_engine(ha: MagicMock | None = None) -> CycleEngine:
    if ha is None:
        ha = _make_ha()
    return CycleEngine(
        thermostat_entity_id=THERMO_ID,
        ha=ha,
        vent_ctrl=VentController(ha),
        get_enabled=lambda: True,
    )


async def _setup_db_and_room(room_id: str = "r1", room_name: str = "Bedroom"):
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    room = Room(id=room_id, name=room_name, thermostat_entity_id=THERMO_ID)
    await db.upsert_room(conn, room)
    from backend.models import RoomSensor

    sensor = RoomSensor.create(room_id=room.id, entity_id=f"sensor.{room_id}_temp")
    await db.add_room_sensor(conn, sensor)
    await db.upsert_thermostat_config(conn, ThermostatConfig(thermostat_entity_id=THERMO_ID))
    return conn, room


# ---------------------------------------------------------------------------
# Cycle start snapshot
# ---------------------------------------------------------------------------


class TestCycleStartSnapshot:
    @pytest.mark.asyncio
    async def test_cycle_start_captures_thermo_temp_setpoint_vents(self):
        conn, room = await _setup_db_and_room()

        # Add a vent so vents_at_start is populated
        vent = RoomVent.create(room.id, "cover.vent_1")
        await db.add_room_vent(conn, vent)

        ha = _make_ha(ambient=78.5, setpoint=76.0)
        # Pretend the vent is currently open in HA before the engine acts
        ha.get_state.side_effect = lambda eid: (
            {
                "state": "cool",
                "attributes": {
                    "current_temperature": 78.5,
                    "temperature": 76.0,
                    "hvac_action": "cooling",
                },
            }
            if eid == THERMO_ID
            else {"state": "open", "attributes": {}}
        )
        ha.get_numeric_state.return_value = 78.0  # so temp_at_start is set

        engine = _make_engine(ha)
        await engine.load_room_sensors(conn, [room.id])

        active_map = {
            room.id: ActiveRoom(room=room, target_temp=72.0, source="schedule"),
        }
        await engine._start_or_update_cycle(conn, active_map, "cooling")

        cycle = engine._cycle_log
        assert cycle is not None
        assert cycle.thermostat_temp_at_start == pytest.approx(78.5)
        assert cycle.setpoint_at_start == pytest.approx(76.0)
        assert cycle.vents_at_start is not None
        vents_map = json.loads(cycle.vents_at_start)
        assert vents_map["cover.vent_1"] == "open"

        # Per-room state
        rcs = engine._room_cycle_states[room.id]
        assert rcs.temp_at_start == pytest.approx(78.0)
        assert rcs.trigger_detail is not None
        detail = json.loads(rcs.trigger_detail)
        assert detail["source"] == "schedule"
        assert detail["target"] == 72.0
        assert rcs.joined_at is None  # present at start

        # DB was persisted
        db_cycle = await db.get_cycle_log(conn, cycle.id)
        assert db_cycle is not None
        assert db_cycle.thermostat_temp_at_start == pytest.approx(78.5)
        assert db_cycle.setpoint_at_start == pytest.approx(76.0)
        assert db_cycle.vents_at_start is not None

        # Vent events: opened_at_start written
        events = await db.get_cycle_vent_events(conn, cycle.id)
        assert any(e.action == "opened_at_start" for e in events)
        assert any(e.entity_id == "cover.vent_1" for e in events)

        await conn.close()

    @pytest.mark.asyncio
    async def test_schedule_trigger_detail_includes_times(self):
        conn, room = await _setup_db_and_room()
        # Create a schedule that matches right now so trigger_detail picks it up
        now = datetime.now()
        schedule = Schedule.create(
            room_id=room.id,
            days_of_week=list(range(7)),
            start_time=(now - timedelta(hours=1)).time(),
            end_time=(now + timedelta(hours=1)).time(),
            target_temp=72.0,
        )
        await db.upsert_schedule(conn, schedule)

        ha = _make_ha()
        engine = _make_engine(ha)
        await engine.load_room_sensors(conn, [room.id])

        active_map = {
            room.id: ActiveRoom(room=room, target_temp=72.0, source="schedule"),
        }
        await engine._start_or_update_cycle(conn, active_map, "cooling")

        rcs = engine._room_cycle_states[room.id]
        detail = json.loads(rcs.trigger_detail)
        assert detail["source"] == "schedule"
        assert detail["schedule_id"] == schedule.id
        assert "start_time" in detail
        assert "end_time" in detail

        await conn.close()


# ---------------------------------------------------------------------------
# Termination / abort / timeout
# ---------------------------------------------------------------------------


async def _running_cycle_with_vent():
    conn, room = await _setup_db_and_room()
    vent = RoomVent.create(room.id, "cover.vent_1")
    await db.add_room_vent(conn, vent)

    ha = _make_ha(ambient=70.0, setpoint=68.0)
    # Vent is "closed" at end so it shows up in vents_at_end
    ha.get_state.side_effect = lambda eid: (
        {
            "state": "cool",
            "attributes": {
                "current_temperature": 70.0,
                "temperature": 68.0,
                "hvac_action": "idle",
            },
        }
        if eid == THERMO_ID
        else {"state": "closed", "attributes": {}}
    )

    engine = _make_engine(ha)
    await engine.load_room_sensors(conn, [room.id])

    cycle = CycleLog.create(
        thermostat_entity_id=THERMO_ID,
        mode="cooling",
        rooms_json=json.dumps({room.id: {"name": room.name, "target": 72.0}}),
    )
    await db.insert_cycle_log(conn, cycle)
    rcs = RoomCycleState(cycle_id=cycle.id, room_id=room.id, target_temp=72.0)
    await db.upsert_room_cycle_state(conn, rcs)

    engine._state = CycleState.RUNNING
    engine._cycle_log = cycle
    engine._cycle_mode = "cooling"
    engine._cycle_ha_mode = "cool"
    engine._active_rooms = {
        room.id: ActiveRoom(room=room, target_temp=72.0, source="schedule"),
    }
    engine._room_cycle_states = {room.id: rcs}
    engine._room_vents = {room.id: [vent]}
    return engine, conn, cycle, room


class TestCycleEndReasons:
    @pytest.mark.asyncio
    async def test_terminate_writes_completed_reason_and_end_state(self):
        engine, conn, cycle, _room = await _running_cycle_with_vent()

        await engine._terminate_cycle(conn)

        db_cycle = await db.get_cycle_log(conn, cycle.id)
        assert db_cycle is not None
        assert db_cycle.ended_at is not None
        assert db_cycle.ended_reason == "completed"
        assert db_cycle.thermostat_temp_at_end == pytest.approx(70.0)
        assert db_cycle.setpoint_at_end == pytest.approx(68.0)
        assert db_cycle.vents_at_end is not None
        vents_end = json.loads(db_cycle.vents_at_end)
        assert vents_end["cover.vent_1"] == "closed"

        await conn.close()

    @pytest.mark.asyncio
    async def test_terminate_with_reason_param(self):
        engine, conn, cycle, _room = await _running_cycle_with_vent()

        await engine._terminate_cycle(conn, reason="timeout")

        db_cycle = await db.get_cycle_log(conn, cycle.id)
        assert db_cycle.ended_reason == "timeout"
        await conn.close()

    @pytest.mark.asyncio
    async def test_abort_writes_aborted_prefix(self):
        engine, conn, cycle, _room = await _running_cycle_with_vent()

        await engine._abort_cycle(conn, reason="system disabled")

        db_cycle = await db.get_cycle_log(conn, cycle.id)
        assert db_cycle is not None
        assert db_cycle.ended_reason == "aborted: system disabled"
        assert db_cycle.vents_at_end is not None

        await conn.close()


# ---------------------------------------------------------------------------
# Temp samples
# ---------------------------------------------------------------------------


class TestTempSampling:
    @pytest.mark.asyncio
    async def test_monitor_rooms_inserts_temp_sample(self):
        engine, conn, cycle, room = await _running_cycle_with_vent()
        engine._ha.get_numeric_state.return_value = 74.0  # room avg

        await engine._monitor_rooms(conn, "cooling")

        samples = await db.get_cycle_temp_samples(conn, cycle.id, room_id=room.id)
        assert len(samples) >= 1
        s = samples[-1]
        assert s.room_temp == pytest.approx(74.0)
        assert s.thermostat_temp == pytest.approx(70.0)
        assert s.setpoint == pytest.approx(68.0)

        await conn.close()


# ---------------------------------------------------------------------------
# Setpoint history
# ---------------------------------------------------------------------------


class TestSetpointHistory:
    @pytest.mark.asyncio
    async def test_set_setpoint_writes_history(self):
        engine, conn, cycle, _room = await _running_cycle_with_vent()
        tc = ThermostatConfig(thermostat_entity_id=THERMO_ID, overshoot_delta=2.0)

        await engine._set_thermostat_setpoint(tc, "cooling", conn=conn)

        history = await db.get_cycle_setpoint_history(conn, cycle.id)
        assert len(history) == 1
        entry = history[0]
        assert entry.setpoint is not None
        assert entry.reason == "mode=cooling"

        await conn.close()

    @pytest.mark.asyncio
    async def test_set_setpoint_without_conn_does_not_error(self):
        """Tests that still pass tc alone (no conn) don't fail."""
        engine, conn, _cycle, _room = await _running_cycle_with_vent()
        tc = ThermostatConfig(thermostat_entity_id=THERMO_ID, overshoot_delta=2.0)

        # No conn — should not raise
        await engine._set_thermostat_setpoint(tc, "cooling")

        await conn.close()


# ---------------------------------------------------------------------------
# Vent events
# ---------------------------------------------------------------------------


class TestVentEvents:
    @pytest.mark.asyncio
    async def test_closed_reached_target_event(self):
        engine, conn, cycle, room = await _running_cycle_with_vent()
        # Simulate room now at target so _monitor_rooms decides to close.
        engine._ha.get_numeric_state.return_value = 72.0
        # Vent returns "open" so close_room_vents will actually close it.
        engine._ha.get_state.side_effect = lambda eid: (
            {
                "state": "cool",
                "attributes": {
                    "current_temperature": 70.0,
                    "temperature": 68.0,
                    "hvac_action": "idle",
                },
            }
            if eid == THERMO_ID
            else {"state": "open", "attributes": {}}
        )

        await engine._monitor_rooms(conn, "cooling")

        events = await db.get_cycle_vent_events(conn, cycle.id)
        assert any(e.action == "closed_reached_target" for e in events)

        # temp_at_end also populated
        rcs_rows = await db.get_room_cycle_states(conn, cycle.id)
        assert rcs_rows[0].temp_at_end == pytest.approx(72.0)

        await conn.close()

    @pytest.mark.asyncio
    async def test_force_reopened_event(self):
        engine, conn, cycle, room = await _running_cycle_with_vent()
        # Configure short max_vent_closed_min so the safety trips.
        tc = ThermostatConfig(
            thermostat_entity_id=THERMO_ID, max_vent_closed_min=1, overshoot_delta=2.0
        )
        await db.upsert_thermostat_config(conn, tc)

        # Pretend vent has been closed for >1 min already.
        rcs = engine._room_cycle_states[room.id]
        rcs.vent_closed_at = datetime.utcnow() - timedelta(minutes=5)
        await db.upsert_room_cycle_state(conn, rcs)

        engine._ha.get_numeric_state.return_value = 70.0

        await engine._monitor_rooms(conn, "cooling")

        events = await db.get_cycle_vent_events(conn, cycle.id)
        assert any(e.action == "force_reopened_max_closed" for e in events)

        await conn.close()
