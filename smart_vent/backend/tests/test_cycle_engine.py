"""
Tests for the HVAC cycle engine — covering bugs from issue #48.

Each test targets a specific bug fix to prevent regressions:
  Bug 1: Setpoint clamped against thermostat ambient
  Bug 2: Reject unexpected hvac_mode in setpoint calculation
  Bug 3: Filter opposite-direction rooms from cycle
  Bug 4: Cross-thermostat duplicate cycle prevention (DB level)
  Bug 6: _is_at_target and _monitor_rooms guard against invalid modes
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from backend.engine.cycle_engine import CycleEngine, _is_at_target
from backend.engine.room_manager import ActiveRoom
from backend.engine.vent_controller import VentController
from backend.models import (
    CycleLog,
    Room,
    RoomCycleState,
    ThermostatConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

THERMO_ID = "climate.test_thermostat"


def _make_ha(
    ambient: float = 72.0,
    hvac_mode: str = "cool",
    hvac_action: str = "cooling",
) -> MagicMock:
    """Build a mock HAClient with a thermostat state in the cache."""
    ha = MagicMock()
    ha.get_state.return_value = {
        "state": hvac_mode,
        "attributes": {
            "current_temperature": ambient,
            "temperature": ambient,
            "hvac_action": hvac_action,
        },
    }
    ha.get_numeric_state.return_value = None
    ha.set_thermostat_temperature = AsyncMock()
    ha.open_cover = AsyncMock()
    ha.close_cover = AsyncMock()
    return ha


def _make_room(
    room_id: str = "room1",
    name: str = "Bedroom",
    temp_offset: float = 0.0,
) -> Room:
    return Room(
        id=room_id,
        name=name,
        thermostat_entity_id=THERMO_ID,
        temp_offset=temp_offset,
    )


def _make_tc(**overrides) -> ThermostatConfig:
    defaults = {
        "thermostat_entity_id": THERMO_ID,
        "overshoot_delta": 2.0,
        "deadband": 0.5,
        "min_open_vents": 1,
        "min_setpoint": 60.0,
        "max_setpoint": 85.0,
    }
    defaults.update(overrides)
    return ThermostatConfig(**defaults)


def _make_engine(ha: MagicMock | None = None) -> CycleEngine:
    if ha is None:
        ha = _make_ha()
    vent_ctrl = VentController(ha)
    return CycleEngine(
        thermostat_entity_id=THERMO_ID,
        ha=ha,
        vent_ctrl=vent_ctrl,
        get_enabled=lambda: True,
    )


# ---------------------------------------------------------------------------
# Bug 1: Setpoint clamped against thermostat ambient
# ---------------------------------------------------------------------------


class TestSetpointAmbientClamping:
    """_set_thermostat_setpoint must clamp setpoint beyond ambient."""

    @pytest.mark.asyncio
    async def test_cooling_setpoint_clamped_when_above_ambient(self):
        """Cooling: setpoint should be <= ambient - overshoot_delta."""
        ha = _make_ha(ambient=71.0)
        engine = _make_engine(ha)
        tc = _make_tc(overshoot_delta=2.0)

        # Room target 74 → unclamped setpoint = 74 - 2 = 72, but ambient is 71
        # so clamp to 71 - 2 = 69
        room = _make_room()
        engine._active_rooms = {
            "room1": ActiveRoom(room=room, target_temp=74.0, source="schedule"),
        }

        await engine._set_thermostat_setpoint(tc, "cooling")

        call_args = ha.set_thermostat_temperature.call_args
        setpoint = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]["temperature"]
        assert setpoint == 69.0, f"Expected 69.0, got {setpoint}"

    @pytest.mark.asyncio
    async def test_heating_setpoint_clamped_when_below_ambient(self):
        """Heating: setpoint should be >= ambient + overshoot_delta."""
        ha = _make_ha(ambient=75.0)
        engine = _make_engine(ha)
        tc = _make_tc(overshoot_delta=2.0)

        # Room target 72 → unclamped setpoint = 72 + 2 = 74, but ambient is 75
        # so clamp to 75 + 2 = 77
        room = _make_room()
        engine._active_rooms = {
            "room1": ActiveRoom(room=room, target_temp=72.0, source="schedule"),
        }

        await engine._set_thermostat_setpoint(tc, "heating")

        call_args = ha.set_thermostat_temperature.call_args
        setpoint = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]["temperature"]
        assert setpoint == 77.0, f"Expected 77.0, got {setpoint}"

    @pytest.mark.asyncio
    async def test_no_clamping_when_setpoint_already_beyond_ambient(self):
        """No clamping needed when target-derived setpoint is already correct."""
        ha = _make_ha(ambient=80.0)
        engine = _make_engine(ha)
        tc = _make_tc(overshoot_delta=2.0)

        # Room target 74 → setpoint = 74 - 2 = 72, ambient 80 → 72 < 80 - 2 = 78 ✓
        room = _make_room()
        engine._active_rooms = {
            "room1": ActiveRoom(room=room, target_temp=74.0, source="schedule"),
        }

        await engine._set_thermostat_setpoint(tc, "cooling")

        call_args = ha.set_thermostat_temperature.call_args
        setpoint = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]["temperature"]
        assert setpoint == 72.0, f"Expected 72.0, got {setpoint}"


# ---------------------------------------------------------------------------
# Bug 2: Reject unexpected hvac_mode in setpoint
# ---------------------------------------------------------------------------


class TestSetpointModeRejection:
    """_set_thermostat_setpoint must reject unexpected mode values."""

    @pytest.mark.asyncio
    async def test_off_mode_rejected(self):
        """Mode 'off' should not set a setpoint (no HA call)."""
        ha = _make_ha()
        engine = _make_engine(ha)
        tc = _make_tc()
        room = _make_room()
        engine._active_rooms = {
            "room1": ActiveRoom(room=room, target_temp=74.0, source="schedule"),
        }

        await engine._set_thermostat_setpoint(tc, "off")

        ha.set_thermostat_temperature.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_mode_rejected(self):
        """Mode 'unknown' should not set a setpoint (no HA call)."""
        ha = _make_ha()
        engine = _make_engine(ha)
        tc = _make_tc()
        room = _make_room()
        engine._active_rooms = {
            "room1": ActiveRoom(room=room, target_temp=74.0, source="schedule"),
        }

        await engine._set_thermostat_setpoint(tc, "unknown")

        ha.set_thermostat_temperature.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooling_mode_accepted(self):
        """Mode 'cooling' should set a setpoint."""
        ha = _make_ha(ambient=80.0)
        engine = _make_engine(ha)
        tc = _make_tc()
        room = _make_room()
        engine._active_rooms = {
            "room1": ActiveRoom(room=room, target_temp=74.0, source="schedule"),
        }

        await engine._set_thermostat_setpoint(tc, "cooling")

        ha.set_thermostat_temperature.assert_called_once()

    @pytest.mark.asyncio
    async def test_heating_mode_accepted(self):
        """Mode 'heating' should set a setpoint."""
        ha = _make_ha(ambient=65.0)
        engine = _make_engine(ha)
        tc = _make_tc()
        room = _make_room()
        engine._active_rooms = {
            "room1": ActiveRoom(room=room, target_temp=72.0, source="schedule"),
        }

        await engine._set_thermostat_setpoint(tc, "heating")

        ha.set_thermostat_temperature.assert_called_once()


# ---------------------------------------------------------------------------
# Bug 3: Filter opposite-direction rooms from cycle
# ---------------------------------------------------------------------------


class TestFilterRoomsForMode:
    """_filter_rooms_for_mode should exclude rooms needing the opposite direction."""

    @pytest.mark.asyncio
    async def test_cooling_excludes_rooms_needing_heat(self):
        """In a cooling cycle, rooms below target-deadband are excluded."""
        ha = _make_ha(ambient=72.0)
        engine = _make_engine(ha)

        room_cool = _make_room("r1", "Hot Room")
        room_heat = _make_room("r2", "Cold Room")
        active = {
            "r1": ActiveRoom(room=room_cool, target_temp=74.0, source="schedule"),
            "r2": ActiveRoom(room=room_heat, target_temp=74.0, source="schedule"),
        }

        # Mock sensor readings: r1=78 (needs cooling), r2=70 (needs heating)
        def get_numeric(eid):
            return None

        ha.get_numeric_state.side_effect = get_numeric

        # Use thermostat ambient as proxy (72) for both rooms since no sensors
        # With ambient=72, target=74, deadband=0.5:
        # - effective=72 vs target-deadband=73.5 → 72 < 73.5 → needs heat → excluded
        # All rooms use the same ambient fallback so both would be excluded or kept
        # Let's set up with actual sensor map instead
        engine._sensor_map = {"r1": ["sensor.r1"], "r2": ["sensor.r2"]}

        def get_numeric_state(eid):
            if eid == "sensor.r1":
                return 78.0  # needs cooling
            if eid == "sensor.r2":
                return 70.0  # needs heating (below 74 - 0.5)
            return None

        ha.get_numeric_state.side_effect = get_numeric_state

        thermo_state = ha.get_state(THERMO_ID)
        result = await engine._filter_rooms_for_mode(active, "cooling", 0.5, thermo_state)

        assert "r1" in result, "Hot room should be kept in cooling cycle"
        assert "r2" not in result, "Cold room should be excluded from cooling cycle"

    @pytest.mark.asyncio
    async def test_heating_excludes_rooms_needing_cool(self):
        """In a heating cycle, rooms above target+deadband are excluded."""
        ha = _make_ha(ambient=72.0)
        engine = _make_engine(ha)

        room_heat = _make_room("r1", "Cold Room")
        room_cool = _make_room("r2", "Hot Room")
        active = {
            "r1": ActiveRoom(room=room_heat, target_temp=74.0, source="schedule"),
            "r2": ActiveRoom(room=room_cool, target_temp=74.0, source="schedule"),
        }

        engine._sensor_map = {"r1": ["sensor.r1"], "r2": ["sensor.r2"]}

        def get_numeric_state(eid):
            if eid == "sensor.r1":
                return 70.0  # needs heating
            if eid == "sensor.r2":
                return 80.0  # needs cooling (above 74 + 0.5)
            return None

        ha.get_numeric_state.side_effect = get_numeric_state

        thermo_state = ha.get_state(THERMO_ID)
        result = await engine._filter_rooms_for_mode(active, "heating", 0.5, thermo_state)

        assert "r1" in result, "Cold room should be kept in heating cycle"
        assert "r2" not in result, "Hot room should be excluded from heating cycle"

    @pytest.mark.asyncio
    async def test_within_deadband_rooms_kept(self):
        """Rooms within deadband are kept regardless of mode."""
        ha = _make_ha(ambient=74.0)
        engine = _make_engine(ha)

        room = _make_room("r1", "Neutral Room")
        active = {
            "r1": ActiveRoom(room=room, target_temp=74.0, source="schedule"),
        }
        engine._sensor_map = {"r1": ["sensor.r1"]}
        ha.get_numeric_state.return_value = 74.2  # within deadband of 0.5

        thermo_state = ha.get_state(THERMO_ID)
        result = await engine._filter_rooms_for_mode(active, "cooling", 0.5, thermo_state)

        assert "r1" in result, "Room within deadband should be kept"

    @pytest.mark.asyncio
    async def test_no_sensor_data_rooms_kept(self):
        """Rooms with no sensor data are kept (benefit of the doubt)."""
        ha = _make_ha()
        # Return None for thermostat state too → no ambient fallback
        ha.get_state.return_value = None
        engine = _make_engine(ha)

        room = _make_room("r1", "No Sensors")
        active = {
            "r1": ActiveRoom(room=room, target_temp=74.0, source="schedule"),
        }
        engine._sensor_map = {"r1": []}

        result = await engine._filter_rooms_for_mode(active, "cooling", 0.5, None)

        assert "r1" in result, "Room with no data should be kept"


# ---------------------------------------------------------------------------
# Bug 4: Cross-thermostat duplicate cycle prevention (DB)
# ---------------------------------------------------------------------------


class TestCrossThermoDuplicatePrevention:
    """close_open_cycles_for_rooms should close cycles on other thermostats."""

    @pytest.mark.asyncio
    async def test_closes_cycle_on_other_thermostat(self):
        """A room in an open cycle on thermostat A is cleaned up when thermostat B starts."""
        from backend import db

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)

        # Insert the room record (FK target for room_cycle_states)
        room = Room.create(name="Room 1", thermostat_entity_id="climate.thermo_a")
        room.id = "room1"
        await db.upsert_room(conn, room)

        # Insert an open cycle on thermostat A containing room1
        cycle_a = CycleLog.create(
            thermostat_entity_id="climate.thermo_a",
            mode="cooling",
            rooms_json=json.dumps({"room1": {"name": "Room 1", "target": 74.0}}),
        )
        await db.insert_cycle_log(conn, cycle_a)
        rcs = RoomCycleState(cycle_id=cycle_a.id, room_id="room1", target_temp=74.0)
        await db.upsert_room_cycle_state(conn, rcs)

        # Close cycles for room1 on other thermostats (excluding thermo_b)
        closed = await db.close_open_cycles_for_rooms(
            conn, ["room1"], exclude_thermostat="climate.thermo_b"
        )

        assert closed == 1, f"Expected 1 cycle closed, got {closed}"

        # Verify cycle A is now closed
        open_logs = await db.get_open_cycle_logs(conn, "climate.thermo_a")
        assert len(open_logs) == 0, "Cycle A should be closed"

        await conn.close()

    @pytest.mark.asyncio
    async def test_does_not_close_own_thermostat(self):
        """Cycles on the excluded thermostat should not be closed."""
        from backend import db

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)

        # Insert the room record (FK target for room_cycle_states)
        room = Room.create(name="Room 1", thermostat_entity_id="climate.thermo_a")
        room.id = "room1"
        await db.upsert_room(conn, room)

        # Insert an open cycle on thermostat A
        cycle_a = CycleLog.create(
            thermostat_entity_id="climate.thermo_a",
            mode="cooling",
            rooms_json=json.dumps({"room1": {"name": "Room 1", "target": 74.0}}),
        )
        await db.insert_cycle_log(conn, cycle_a)
        rcs = RoomCycleState(cycle_id=cycle_a.id, room_id="room1", target_temp=74.0)
        await db.upsert_room_cycle_state(conn, rcs)

        # Try to close, but exclude thermo_a (our own thermostat)
        closed = await db.close_open_cycles_for_rooms(
            conn, ["room1"], exclude_thermostat="climate.thermo_a"
        )

        assert closed == 0, "Should not close own thermostat's cycle"

        open_logs = await db.get_open_cycle_logs(conn, "climate.thermo_a")
        assert len(open_logs) == 1, "Cycle A should still be open"

        await conn.close()

    @pytest.mark.asyncio
    async def test_empty_room_ids_noop(self):
        """Passing no room IDs should be a no-op."""
        from backend import db

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)

        closed = await db.close_open_cycles_for_rooms(conn, [])
        assert closed == 0

        await conn.close()


# ---------------------------------------------------------------------------
# Bug 6: _is_at_target and monitor mode guard
# ---------------------------------------------------------------------------


class TestIsAtTarget:
    """_is_at_target must handle modes explicitly."""

    def test_cooling_at_target(self):
        assert _is_at_target(73.0, 74.0, "cooling", 0.5) is True  # 73 <= 74.5

    def test_cooling_not_at_target(self):
        assert _is_at_target(76.0, 74.0, "cooling", 0.5) is False  # 76 > 74.5

    def test_heating_at_target(self):
        assert _is_at_target(74.0, 74.0, "heating", 0.5) is True  # 74 >= 73.5

    def test_heating_not_at_target(self):
        assert _is_at_target(72.0, 74.0, "heating", 0.5) is False  # 72 < 73.5

    def test_off_mode_returns_false(self):
        """Unexpected mode 'off' should return False (safe — vents stay open)."""
        assert _is_at_target(74.0, 74.0, "off", 0.5) is False

    def test_unknown_mode_returns_false(self):
        """Unexpected mode 'unknown' should return False."""
        assert _is_at_target(74.0, 74.0, "unknown", 0.5) is False
