"""
Tests for the HVAC cycle engine.

Covers:
  - Issue #48 bug-fix regressions (Bugs 1–6)
  - _read_hvac_mode: HA state interpretation
  - _infer_mode_from_room_temps: majority vote, sensor fallback, ambient sanity
  - _get_avg_temp: sensor aggregation, thermostat sensor inclusion
  - Setpoint multi-room target selection (min for cooling, max for heating)
  - _is_at_target: deadband boundary precision
  - _filter_rooms_for_mode: temp_offset handling
  - Cross-thermostat duplicate cycle prevention (DB level)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from backend.engine.cycle_engine import CycleEngine, CycleState, _is_at_target
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


def _make_tc(**overrides: object) -> ThermostatConfig:
    defaults: dict[str, object] = {
        "thermostat_entity_id": THERMO_ID,
        "overshoot_delta": 2.0,
        "deadband": 0.5,
        "min_open_vents": 1,
        "min_setpoint": 60.0,
        "max_setpoint": 85.0,
    }
    defaults.update(overrides)
    return ThermostatConfig(**defaults)  # type: ignore[arg-type]


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
    """_is_at_target must handle modes explicitly and not use deadband."""

    def test_cooling_at_target(self):
        assert _is_at_target(73.0, 74.0, "cooling") is True  # 73 <= 74

    def test_cooling_not_at_target(self):
        """Cooling above target -> not at target (even if within deadband)."""
        assert _is_at_target(74.5, 74.0, "cooling") is False  # 74.5 > 74

    def test_heating_at_target(self):
        assert _is_at_target(75.0, 74.0, "heating") is True  # 75 >= 74

    def test_heating_not_at_target(self):
        """Heating below target -> not at target (even if within deadband)."""
        assert _is_at_target(73.5, 74.0, "heating") is False  # 73.5 < 74

    def test_off_mode_returns_false(self):
        """Unexpected mode 'off' should return False (safe — vents stay open)."""
        assert _is_at_target(74.0, 74.0, "off") is False

    def test_unknown_mode_returns_false(self):
        """Unexpected mode 'unknown' should return False."""
        assert _is_at_target(74.0, 74.0, "unknown") is False

    def test_cooling_exact_boundary(self):
        """Cooling at exactly target → at target (inclusive)."""
        assert _is_at_target(74.0, 74.0, "cooling") is True

    def test_heating_exact_boundary(self):
        """Heating at exactly target → at target (inclusive)."""
        assert _is_at_target(74.0, 74.0, "heating") is True


# ---------------------------------------------------------------------------
# _read_hvac_mode
# ---------------------------------------------------------------------------


class TestReadHvacMode:
    """_read_hvac_mode interprets HA thermostat state correctly."""

    def test_action_heating_returns_heating(self):
        ha = _make_ha(hvac_mode="heat", hvac_action="heating")
        engine = _make_engine(ha)
        assert engine._read_hvac_mode() == "heating"

    def test_action_cooling_returns_cooling(self):
        ha = _make_ha(hvac_mode="cool", hvac_action="cooling")
        engine = _make_engine(ha)
        assert engine._read_hvac_mode() == "cooling"

    def test_action_idle_mode_heat_returns_heating(self):
        """Single-direction mode 'heat' with idle action → heating."""
        ha = _make_ha(hvac_mode="heat", hvac_action="idle")
        engine = _make_engine(ha)
        assert engine._read_hvac_mode() == "heating"

    def test_action_idle_mode_cool_returns_cooling(self):
        """Single-direction mode 'cool' with idle action → cooling."""
        ha = _make_ha(hvac_mode="cool", hvac_action="idle")
        engine = _make_engine(ha)
        assert engine._read_hvac_mode() == "cooling"

    def test_action_idle_mode_heat_cool_returns_off(self):
        """heat_cool mode with idle action → off (direction unknown)."""
        ha = _make_ha(hvac_mode="heat_cool", hvac_action="idle")
        engine = _make_engine(ha)
        assert engine._read_hvac_mode() == "off"

    def test_mode_off_returns_off(self):
        ha = _make_ha(hvac_mode="off", hvac_action="off")
        engine = _make_engine(ha)
        assert engine._read_hvac_mode() == "off"

    def test_no_state_returns_unknown(self):
        ha = _make_ha()
        ha.get_state.return_value = None
        engine = _make_engine(ha)
        assert engine._read_hvac_mode() == "unknown"

    def test_action_takes_priority_over_mode(self):
        """hvac_action 'cooling' overrides hvac_mode 'heat'."""
        ha = _make_ha(hvac_mode="heat", hvac_action="cooling")
        engine = _make_engine(ha)
        assert engine._read_hvac_mode() == "cooling"


# ---------------------------------------------------------------------------
# _infer_mode_from_room_temps
# ---------------------------------------------------------------------------


class TestInferMode:
    """Mode inference: majority vote, fallback, sanity check."""

    @pytest.mark.asyncio
    async def test_all_rooms_need_cooling(self):
        ha = _make_ha(ambient=78.0)
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["s1"], "r2": ["s2"]}
        ha.get_numeric_state.side_effect = lambda eid: {"s1": 80.0, "s2": 79.0}.get(eid)

        rooms = {
            "r1": ActiveRoom(room=_make_room("r1"), target_temp=74.0, source="schedule"),
            "r2": ActiveRoom(room=_make_room("r2"), target_temp=74.0, source="schedule"),
        }
        thermo = ha.get_state(THERMO_ID)
        result = await engine._infer_mode_from_room_temps(rooms, 0.5, thermo)
        assert result == "cooling"

    @pytest.mark.asyncio
    async def test_all_rooms_need_heating(self):
        ha = _make_ha(ambient=68.0)
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["s1"], "r2": ["s2"]}
        ha.get_numeric_state.side_effect = lambda eid: {"s1": 65.0, "s2": 66.0}.get(eid)

        rooms = {
            "r1": ActiveRoom(room=_make_room("r1"), target_temp=74.0, source="schedule"),
            "r2": ActiveRoom(room=_make_room("r2"), target_temp=74.0, source="schedule"),
        }
        thermo = ha.get_state(THERMO_ID)
        result = await engine._infer_mode_from_room_temps(rooms, 0.5, thermo)
        assert result == "heating"

    @pytest.mark.asyncio
    async def test_all_rooms_within_deadband_returns_off(self):
        ha = _make_ha(ambient=74.0)
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["s1"]}
        ha.get_numeric_state.return_value = 74.2

        rooms = {
            "r1": ActiveRoom(room=_make_room("r1"), target_temp=74.0, source="schedule"),
        }
        thermo = ha.get_state(THERMO_ID)
        result = await engine._infer_mode_from_room_temps(rooms, 0.5, thermo)
        assert result == "off"

    @pytest.mark.asyncio
    async def test_mixed_rooms_majority_wins(self):
        """2 rooms need cooling, 1 needs heating → cooling."""
        ha = _make_ha(ambient=74.0)
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["s1"], "r2": ["s2"], "r3": ["s3"]}
        ha.get_numeric_state.side_effect = lambda eid: {
            "s1": 80.0,  # needs cool
            "s2": 79.0,  # needs cool
            "s3": 65.0,  # needs heat
        }.get(eid)

        rooms = {
            "r1": ActiveRoom(room=_make_room("r1"), target_temp=74.0, source="schedule"),
            "r2": ActiveRoom(room=_make_room("r2"), target_temp=74.0, source="schedule"),
            "r3": ActiveRoom(room=_make_room("r3"), target_temp=74.0, source="schedule"),
        }
        thermo = ha.get_state(THERMO_ID)
        result = await engine._infer_mode_from_room_temps(rooms, 0.5, thermo)
        assert result == "cooling"

    @pytest.mark.asyncio
    async def test_tie_goes_to_cooling(self):
        """Equal cool/heat votes → cooling wins."""
        ha = _make_ha(ambient=74.0)
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["s1"], "r2": ["s2"]}
        ha.get_numeric_state.side_effect = lambda eid: {
            "s1": 80.0,  # needs cool
            "s2": 65.0,  # needs heat
        }.get(eid)

        rooms = {
            "r1": ActiveRoom(room=_make_room("r1"), target_temp=74.0, source="schedule"),
            "r2": ActiveRoom(room=_make_room("r2"), target_temp=74.0, source="schedule"),
        }
        thermo = ha.get_state(THERMO_ID)
        result = await engine._infer_mode_from_room_temps(rooms, 0.5, thermo)
        assert result == "cooling"

    @pytest.mark.asyncio
    async def test_sensor_fallback_uses_thermostat_ambient(self):
        """Room with no sensor data uses thermostat ambient as proxy."""
        ha = _make_ha(ambient=80.0)  # ambient says hot
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": []}  # no sensors
        ha.get_numeric_state.return_value = None

        rooms = {
            "r1": ActiveRoom(room=_make_room("r1"), target_temp=74.0, source="schedule"),
        }
        thermo = ha.get_state(THERMO_ID)
        result = await engine._infer_mode_from_room_temps(rooms, 0.5, thermo)
        # 80 > 74 + 0.5 → needs cooling
        assert result == "cooling"

    @pytest.mark.asyncio
    async def test_ambient_sanity_check_flips_mode(self):
        """Room sensors vote heating but thermostat ambient contradicts → cooling."""
        ha = _make_ha(ambient=85.0)  # thermostat says very hot
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["s1"]}
        # Sensor says cold (stale/misplaced), votes heating
        ha.get_numeric_state.return_value = 65.0

        rooms = {
            "r1": ActiveRoom(room=_make_room("r1"), target_temp=74.0, source="schedule"),
        }
        thermo = ha.get_state(THERMO_ID)
        # Sensor vote: 65 < 74-0.5 → heating
        # Sanity: ambient 85 > max(targets) + deadband = 74.5 → contradicts → flip to cooling
        result = await engine._infer_mode_from_room_temps(rooms, 0.5, thermo)
        assert result == "cooling"


# ---------------------------------------------------------------------------
# _get_avg_temp
# ---------------------------------------------------------------------------


class TestGetAvgTemp:
    """Temperature aggregation from sensors and thermostat."""

    def test_single_sensor(self):
        ha = _make_ha(ambient=72.0)
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["sensor.r1_temp"]}
        ha.get_numeric_state.return_value = 75.0

        room = _make_room("r1")
        assert engine._get_avg_temp(room) == 75.0

    def test_multiple_sensors_averaged(self):
        ha = _make_ha(ambient=72.0)
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["s1", "s2"]}
        ha.get_numeric_state.side_effect = lambda eid: {"s1": 70.0, "s2": 80.0}.get(eid)

        room = _make_room("r1")
        assert engine._get_avg_temp(room) == 75.0

    def test_include_thermostat_sensor(self):
        ha = _make_ha(ambient=72.0)
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["s1"]}
        ha.get_numeric_state.return_value = 78.0

        room = _make_room("r1")
        room.include_thermostat_sensor = True
        # Sensor=78, thermostat=72 → avg = (78+72)/2 = 75
        assert engine._get_avg_temp(room) == 75.0

    def test_no_sensors_no_thermostat_returns_none(self):
        ha = _make_ha()
        ha.get_state.return_value = None
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": []}

        room = _make_room("r1")
        assert engine._get_avg_temp(room) is None

    def test_unavailable_sensors_skipped(self):
        ha = _make_ha(ambient=72.0)
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["s1", "s2"]}
        ha.get_numeric_state.side_effect = lambda eid: {"s1": 76.0, "s2": None}.get(eid)

        room = _make_room("r1")
        # Only s1 has data → 76.0
        assert engine._get_avg_temp(room) == 76.0


# ---------------------------------------------------------------------------
# Setpoint multi-room target selection
# ---------------------------------------------------------------------------


class TestSetpointTargetSelection:
    """Correct target selection: min(targets) for cooling, max(targets) for heating."""

    @pytest.mark.asyncio
    async def test_cooling_uses_min_target(self):
        """Cooling setpoint derived from min(targets) - overshoot."""
        ha = _make_ha(ambient=80.0)
        engine = _make_engine(ha)
        tc = _make_tc(overshoot_delta=2.0)
        engine._active_rooms = {
            "r1": ActiveRoom(room=_make_room("r1"), target_temp=74.0, source="schedule"),
            "r2": ActiveRoom(room=_make_room("r2"), target_temp=76.0, source="schedule"),
        }

        await engine._set_thermostat_setpoint(tc, "cooling")

        call_args = ha.set_thermostat_temperature.call_args
        setpoint = call_args[0][1]
        # min(74, 76) - 2 = 72
        assert setpoint == 72.0

    @pytest.mark.asyncio
    async def test_heating_uses_max_target(self):
        """Heating setpoint derived from max(targets) + overshoot."""
        ha = _make_ha(ambient=60.0)
        engine = _make_engine(ha)
        tc = _make_tc(overshoot_delta=2.0)
        engine._active_rooms = {
            "r1": ActiveRoom(room=_make_room("r1"), target_temp=72.0, source="schedule"),
            "r2": ActiveRoom(room=_make_room("r2"), target_temp=74.0, source="schedule"),
        }

        await engine._set_thermostat_setpoint(tc, "heating")

        call_args = ha.set_thermostat_temperature.call_args
        setpoint = call_args[0][1]
        # max(72, 74) + 2 = 76
        assert setpoint == 76.0

    @pytest.mark.asyncio
    async def test_empty_rooms_skips_setpoint(self):
        """No active rooms → no setpoint call."""
        ha = _make_ha()
        engine = _make_engine(ha)
        tc = _make_tc()
        engine._active_rooms = {}

        await engine._set_thermostat_setpoint(tc, "cooling")

        ha.set_thermostat_temperature.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooling_sends_cool_ha_mode(self):
        """Cooling sends ha_mode='cool' to HA."""
        ha = _make_ha(ambient=80.0)
        engine = _make_engine(ha)
        tc = _make_tc()
        engine._active_rooms = {
            "r1": ActiveRoom(room=_make_room("r1"), target_temp=74.0, source="schedule"),
        }

        await engine._set_thermostat_setpoint(tc, "cooling")

        call_args = ha.set_thermostat_temperature.call_args
        assert call_args[1].get("hvac_mode") == "cool" or call_args[0][2] == "cool"

    @pytest.mark.asyncio
    async def test_heating_sends_heat_ha_mode(self):
        """Heating sends ha_mode='heat' to HA."""
        ha = _make_ha(ambient=60.0)
        engine = _make_engine(ha)
        tc = _make_tc()
        engine._active_rooms = {
            "r1": ActiveRoom(room=_make_room("r1"), target_temp=74.0, source="schedule"),
        }

        await engine._set_thermostat_setpoint(tc, "heating")

        call_args = ha.set_thermostat_temperature.call_args
        assert call_args[1].get("hvac_mode") == "heat" or call_args[0][2] == "heat"


# ---------------------------------------------------------------------------
# _filter_rooms_for_mode: temp_offset handling
# ---------------------------------------------------------------------------


class TestFilterRoomsTempOffset:
    """Temp offset affects filtering correctly."""

    @pytest.mark.asyncio
    async def test_positive_offset_shifts_effective_temp_up(self):
        """Room with positive offset: effective = avg + offset."""
        ha = _make_ha(ambient=74.0)
        engine = _make_engine(ha)
        # Room sensor reads 73, offset +2 → effective = 75
        # Target 74, deadband 0.5 → 75 > 74.5 → needs cooling → keep in cooling cycle
        room = _make_room("r1", temp_offset=2.0)
        engine._sensor_map = {"r1": ["s1"]}
        ha.get_numeric_state.return_value = 73.0

        active = {"r1": ActiveRoom(room=room, target_temp=74.0, source="schedule")}
        thermo = ha.get_state(THERMO_ID)
        result = await engine._filter_rooms_for_mode(active, "cooling", 0.5, thermo)
        assert "r1" in result

    @pytest.mark.asyncio
    async def test_negative_offset_shifts_effective_temp_down(self):
        """Room with negative offset: effective = avg + offset."""
        ha = _make_ha(ambient=74.0)
        engine = _make_engine(ha)
        # Room sensor reads 75, offset -3 → effective = 72
        # Target 74, deadband 0.5 → 72 < 73.5 → needs heating → exclude from cooling
        room = _make_room("r1", temp_offset=-3.0)
        engine._sensor_map = {"r1": ["s1"]}
        ha.get_numeric_state.return_value = 75.0

        active = {"r1": ActiveRoom(room=room, target_temp=74.0, source="schedule")}
        thermo = ha.get_state(THERMO_ID)
        result = await engine._filter_rooms_for_mode(active, "cooling", 0.5, thermo)
        assert "r1" not in result


# ---------------------------------------------------------------------------
# Engine state properties
# ---------------------------------------------------------------------------


class TestEngineStateProperties:
    """Basic engine state accessors."""

    def test_initial_state_is_idle(self):
        engine = _make_engine()
        assert engine.cycle_state == CycleState.IDLE

    def test_current_cycle_id_none_when_idle(self):
        engine = _make_engine()
        assert engine.current_cycle_id is None

    def test_get_zone_status_idle(self):
        ha = _make_ha(ambient=72.0, hvac_mode="cool", hvac_action="idle")
        engine = _make_engine(ha)
        status = engine.get_zone_status()
        assert status.thermostat_entity_id == THERMO_ID
        assert status.cycle_id is None
        assert status.hvac_action == "idle"


# ---------------------------------------------------------------------------
# _abort_cycle: DB close ordering (issue #51)
# ---------------------------------------------------------------------------


async def _setup_engine_with_running_cycle(ha=None):
    """Create an engine with a RUNNING cycle backed by a real in-memory DB."""
    from backend import db

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)

    if ha is None:
        ha = _make_ha(ambient=72.0)
    engine = _make_engine(ha)

    # Insert room for FK constraints
    room = Room.create(name="Test Room", thermostat_entity_id=THERMO_ID)
    room.id = "r1"
    await db.upsert_room(conn, room)

    # Insert a cycle log and put engine in RUNNING state
    cycle = CycleLog.create(
        thermostat_entity_id=THERMO_ID,
        mode="cooling",
        rooms_json=json.dumps({"r1": {"name": "Test Room", "target": 74.0}}),
    )
    await db.insert_cycle_log(conn, cycle)

    engine._state = CycleState.RUNNING
    engine._cycle_log = cycle
    engine._cycle_mode = "cooling"
    engine._cycle_ha_mode = "cool"
    engine._active_rooms = {
        "r1": ActiveRoom(room=room, target_temp=74.0, source="schedule"),
    }

    return engine, conn, cycle


class TestAbortCycleDbClose:
    """_abort_cycle must close the DB record even when vent/setpoint ops fail."""

    @pytest.mark.asyncio
    async def test_abort_closes_db_record(self):
        """Basic abort should set ended_at in the DB."""
        from backend import db

        engine, conn, cycle = await _setup_engine_with_running_cycle()

        await engine._abort_cycle(conn, reason="test")

        # Verify DB record is closed
        open_logs = await db.get_open_cycle_logs(conn, THERMO_ID)
        assert len(open_logs) == 0, "Cycle should be closed in DB after abort"

        # Verify engine state is reset
        assert engine.cycle_state == CycleState.IDLE
        assert engine._cycle_log is None
        await conn.close()

    @pytest.mark.asyncio
    async def test_abort_closes_db_even_when_vent_open_fails(self):
        """DB record must be closed even if vent operations raise."""
        from backend import db

        ha = _make_ha(ambient=72.0)
        ha.open_cover = AsyncMock(side_effect=RuntimeError("HA unavailable"))
        engine, conn, cycle = await _setup_engine_with_running_cycle(ha)

        # Give the engine some vents so it tries to open them
        from backend.models import RoomVent

        engine._room_vents = {"r1": [RoomVent.create("r1", "cover.vent_1")]}

        await engine._abort_cycle(conn, reason="test")

        open_logs = await db.get_open_cycle_logs(conn, THERMO_ID)
        assert len(open_logs) == 0, "DB record must be closed despite vent failure"
        assert engine.cycle_state == CycleState.IDLE
        await conn.close()

    @pytest.mark.asyncio
    async def test_abort_closes_db_even_when_setpoint_reset_fails(self):
        """DB record must be closed even if setpoint reset raises."""
        from backend import db

        ha = _make_ha(ambient=72.0)
        ha.set_thermostat_temperature = AsyncMock(side_effect=RuntimeError("HA unavailable"))
        engine, conn, cycle = await _setup_engine_with_running_cycle(ha)

        await engine._abort_cycle(conn, reason="test")

        open_logs = await db.get_open_cycle_logs(conn, THERMO_ID)
        assert len(open_logs) == 0, "DB record must be closed despite setpoint failure"
        assert engine.cycle_state == CycleState.IDLE
        await conn.close()

    @pytest.mark.asyncio
    async def test_abort_no_cycle_log_is_noop(self):
        """Aborting with no cycle_log should not crash."""
        engine = _make_engine()
        engine._state = CycleState.RUNNING
        engine._cycle_log = None

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        from backend import db

        await db.init_db(conn)

        await engine._abort_cycle(conn, reason="test")
        assert engine.cycle_state == CycleState.IDLE
        await conn.close()


# ---------------------------------------------------------------------------
# _terminate_cycle: DB close ordering (issue #51)
# ---------------------------------------------------------------------------


class TestTerminateCycleDbClose:
    """_terminate_cycle must close the DB record even when vent/setpoint ops fail."""

    @pytest.mark.asyncio
    async def test_terminate_closes_db_record(self):
        """Normal termination should set ended_at in the DB."""
        from backend import db

        engine, conn, cycle = await _setup_engine_with_running_cycle()

        await engine._terminate_cycle(conn)

        open_logs = await db.get_open_cycle_logs(conn, THERMO_ID)
        assert len(open_logs) == 0, "Cycle should be closed in DB after termination"
        assert engine.cycle_state == CycleState.IDLE
        await conn.close()

    @pytest.mark.asyncio
    async def test_terminate_closes_db_even_when_setpoint_fails(self):
        """DB record must be closed even if setpoint reset raises."""
        from backend import db

        ha = _make_ha(ambient=72.0)
        ha.set_thermostat_temperature = AsyncMock(side_effect=RuntimeError("HA unavailable"))
        engine, conn, cycle = await _setup_engine_with_running_cycle(ha)

        await engine._terminate_cycle(conn)

        open_logs = await db.get_open_cycle_logs(conn, THERMO_ID)
        assert len(open_logs) == 0, "DB record must be closed despite setpoint failure"
        assert engine.cycle_state == CycleState.IDLE
        await conn.close()

    @pytest.mark.asyncio
    async def test_terminate_closes_db_even_when_vent_open_fails(self):
        """DB record must be closed even if vent re-open after termination raises."""
        from backend import db

        ha = _make_ha(ambient=72.0)
        ha.open_cover = AsyncMock(side_effect=RuntimeError("HA unavailable"))
        engine, conn, cycle = await _setup_engine_with_running_cycle(ha)

        from backend.models import RoomVent

        engine._room_vents = {"r1": [RoomVent.create("r1", "cover.vent_1")]}

        await engine._terminate_cycle(conn)

        open_logs = await db.get_open_cycle_logs(conn, THERMO_ID)
        assert len(open_logs) == 0, "DB record must be closed despite vent failure"
        assert engine.cycle_state == CycleState.IDLE
        await conn.close()


# ---------------------------------------------------------------------------
# Short-cycle protection (Issue #208)
# ---------------------------------------------------------------------------


class TestOfftimeLockout:
    """_in_offtime_lockout — compressor minimum off-time between cycles.

    Rapid restart of a compressor is a primary equipment-failure mode. After a
    cycle ends, a new one must not start until min_cycle_offtime_min elapses.
    """

    def test_disabled_when_offtime_zero(self):
        engine = _make_engine()
        engine._last_cycle_ended_at = datetime.now(UTC)
        tc = _make_tc(min_cycle_offtime_min=0)
        assert engine._in_offtime_lockout(tc) is False

    def test_no_lockout_when_no_prior_cycle(self):
        engine = _make_engine()
        assert engine._last_cycle_ended_at is None
        tc = _make_tc(min_cycle_offtime_min=5)
        assert engine._in_offtime_lockout(tc) is False

    def test_locked_out_within_window(self):
        engine = _make_engine()
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        engine._last_cycle_ended_at = now - timedelta(minutes=2)
        tc = _make_tc(min_cycle_offtime_min=5)
        assert engine._in_offtime_lockout(tc, now=now) is True

    def test_not_locked_out_after_window(self):
        engine = _make_engine()
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        engine._last_cycle_ended_at = now - timedelta(minutes=6)
        tc = _make_tc(min_cycle_offtime_min=5)
        assert engine._in_offtime_lockout(tc, now=now) is False

    def test_boundary_exactly_at_limit_is_expired(self):
        engine = _make_engine()
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        engine._last_cycle_ended_at = now - timedelta(minutes=5)
        tc = _make_tc(min_cycle_offtime_min=5)
        # At exactly the limit the lockout has elapsed — a cycle may start.
        assert engine._in_offtime_lockout(tc, now=now) is False

    def test_remaining_minutes_reported(self):
        engine = _make_engine()
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        engine._last_cycle_ended_at = now - timedelta(minutes=2)
        tc = _make_tc(min_cycle_offtime_min=5)
        assert engine._offtime_lockout_remaining(tc, now=now) == pytest.approx(3.0)

    def test_remaining_is_zero_when_not_locked(self):
        engine = _make_engine()
        tc = _make_tc(min_cycle_offtime_min=5)
        assert engine._offtime_lockout_remaining(tc) == 0.0


class TestCycleRuntimeSatisfied:
    """_cycle_runtime_satisfied — compressor minimum run time.

    A cycle that completes seconds after it started has short-cycled the
    compressor. Normal completion is deferred until min_cycle_runtime_min.
    """

    def test_satisfied_when_runtime_zero(self):
        engine = _make_engine()
        engine._cycle_log = CycleLog.create(
            thermostat_entity_id=THERMO_ID, mode="cooling", rooms_json="{}"
        )
        tc = _make_tc(min_cycle_runtime_min=0)
        assert engine._cycle_runtime_satisfied(tc) is True

    def test_satisfied_when_no_cycle_log(self):
        engine = _make_engine()
        engine._cycle_log = None
        tc = _make_tc(min_cycle_runtime_min=10)
        assert engine._cycle_runtime_satisfied(tc) is True

    def test_not_satisfied_before_minimum(self):
        engine = _make_engine()
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        engine._cycle_log = CycleLog.create(
            thermostat_entity_id=THERMO_ID, mode="cooling", rooms_json="{}"
        )
        engine._cycle_log.started_at = now - timedelta(minutes=3)
        tc = _make_tc(min_cycle_runtime_min=10)
        assert engine._cycle_runtime_satisfied(tc, now=now) is False

    def test_satisfied_after_minimum(self):
        engine = _make_engine()
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        engine._cycle_log = CycleLog.create(
            thermostat_entity_id=THERMO_ID, mode="cooling", rooms_json="{}"
        )
        engine._cycle_log.started_at = now - timedelta(minutes=11)
        tc = _make_tc(min_cycle_runtime_min=10)
        assert engine._cycle_runtime_satisfied(tc, now=now) is True


class TestCycleEndRecordsTimestamp:
    """_terminate_cycle and _abort_cycle must record when the cycle ended so
    the off-time lockout can gate the next cycle (Issue #208)."""

    @pytest.mark.asyncio
    async def test_terminate_records_end_timestamp(self):
        engine, conn, _cycle = await _setup_engine_with_running_cycle()
        assert engine._last_cycle_ended_at is None
        await engine._terminate_cycle(conn)
        assert engine._last_cycle_ended_at is not None
        await conn.close()

    @pytest.mark.asyncio
    async def test_abort_records_end_timestamp(self):
        engine, conn, _cycle = await _setup_engine_with_running_cycle()
        assert engine._last_cycle_ended_at is None
        await engine._abort_cycle(conn, reason="test")
        assert engine._last_cycle_ended_at is not None
        await conn.close()


class TestAllActiveRoomsSatisfied:
    """_all_active_rooms_satisfied — detecting when a cycle would terminate,
    so the minimum-runtime hold can engage (Issue #208)."""

    def _engine_with_rooms(self, temps: dict[str, float]) -> CycleEngine:
        ha = _make_ha()
        engine = _make_engine(ha)
        engine._sensor_map = {rid: [f"s_{rid}"] for rid in temps}
        readings = {f"s_{rid}": t for rid, t in temps.items()}
        ha.get_numeric_state.side_effect = lambda eid: readings.get(eid)
        return engine

    def test_all_rooms_at_target_returns_true(self):
        engine = self._engine_with_rooms({"r1": 72.0, "r2": 71.0})
        engine._active_rooms = {
            "r1": ActiveRoom(room=_make_room("r1"), target_temp=72.0, source="schedule"),
            "r2": ActiveRoom(room=_make_room("r2"), target_temp=72.0, source="schedule"),
        }
        engine._room_cycle_states = {
            "r1": RoomCycleState(cycle_id="c", room_id="r1", target_temp=72.0),
            "r2": RoomCycleState(cycle_id="c", room_id="r2", target_temp=72.0),
        }
        assert engine._all_active_rooms_satisfied("cooling") is True

    def test_one_room_not_at_target_returns_false(self):
        engine = self._engine_with_rooms({"r1": 72.0, "r2": 78.0})
        engine._active_rooms = {
            "r1": ActiveRoom(room=_make_room("r1"), target_temp=72.0, source="schedule"),
            "r2": ActiveRoom(room=_make_room("r2"), target_temp=72.0, source="schedule"),
        }
        engine._room_cycle_states = {
            "r1": RoomCycleState(cycle_id="c", room_id="r1", target_temp=72.0),
            "r2": RoomCycleState(cycle_id="c", room_id="r2", target_temp=72.0),
        }
        assert engine._all_active_rooms_satisfied("cooling") is False

    def test_already_closed_room_counts_as_satisfied(self):
        # r1's vent closed earlier — counts as satisfied even though it now
        # reads warm (it has drifted since its vent shut).
        engine = self._engine_with_rooms({"r1": 99.0, "r2": 72.0})
        engine._active_rooms = {
            "r1": ActiveRoom(room=_make_room("r1"), target_temp=72.0, source="schedule"),
            "r2": ActiveRoom(room=_make_room("r2"), target_temp=72.0, source="schedule"),
        }
        rcs1 = RoomCycleState(cycle_id="c", room_id="r1", target_temp=72.0)
        rcs1.vent_closed_at = datetime.now(UTC)
        engine._room_cycle_states = {
            "r1": rcs1,
            "r2": RoomCycleState(cycle_id="c", room_id="r2", target_temp=72.0),
        }
        assert engine._all_active_rooms_satisfied("cooling") is True

    def test_room_without_reading_is_skipped(self):
        # r1 has no sensor reading — it cannot block, mirroring _monitor_rooms.
        engine = self._engine_with_rooms({"r2": 72.0})
        engine._active_rooms = {
            "r1": ActiveRoom(room=_make_room("r1"), target_temp=72.0, source="schedule"),
            "r2": ActiveRoom(room=_make_room("r2"), target_temp=72.0, source="schedule"),
        }
        engine._room_cycle_states = {
            "r1": RoomCycleState(cycle_id="c", room_id="r1", target_temp=72.0),
            "r2": RoomCycleState(cycle_id="c", room_id="r2", target_temp=72.0),
        }
        assert engine._all_active_rooms_satisfied("cooling") is True

    def test_missing_cycle_state_returns_false(self):
        engine = self._engine_with_rooms({"r1": 72.0})
        engine._active_rooms = {
            "r1": ActiveRoom(room=_make_room("r1"), target_temp=72.0, source="schedule"),
        }
        engine._room_cycle_states = {}
        assert engine._all_active_rooms_satisfied("cooling") is False


class TestEnterMinRuntimeHold:
    """_enter_min_runtime_hold — re-open early-closed cycle rooms so the air
    handler is not dead-headed through one room during the hold (Issue #208)."""

    @pytest.mark.asyncio
    async def test_reopens_room_that_closed_early(self):
        from backend import db
        from backend.models import RoomVent

        engine, conn, cycle = await _setup_engine_with_running_cycle()

        # Add a second room; r1 closed early, r2 still open.
        room2 = Room.create(name="Office", thermostat_entity_id=THERMO_ID)
        room2.id = "r2"
        await db.upsert_room(conn, room2)
        engine._active_rooms["r2"] = ActiveRoom(room=room2, target_temp=74.0, source="schedule")
        rcs1 = RoomCycleState(cycle_id=cycle.id, room_id="r1", target_temp=74.0)
        rcs1.vent_closed_at = datetime.now(UTC)
        rcs2 = RoomCycleState(cycle_id=cycle.id, room_id="r2", target_temp=74.0)
        engine._room_cycle_states = {"r1": rcs1, "r2": rcs2}
        await db.upsert_room_cycle_state(conn, rcs1)
        await db.upsert_room_cycle_state(conn, rcs2)
        engine._room_vents = {
            "r1": [RoomVent.create("r1", "cover.vent_r1")],
            "r2": [RoomVent.create("r2", "cover.vent_r2")],
        }

        await engine._enter_min_runtime_hold(conn)

        opened = [c.args[0] for c in engine._ha.open_cover.await_args_list]
        assert "cover.vent_r1" in opened, "the early-closed room's vent must be re-opened"
        assert rcs1.vent_closed_at is None, "vent_closed_at must be cleared on re-open"
        await conn.close()

    @pytest.mark.asyncio
    async def test_noop_when_no_room_was_closed(self):
        from backend.models import RoomVent

        engine, conn, _cycle = await _setup_engine_with_running_cycle()
        # r1's vent_closed_at is None (never closed) — nothing to re-open.
        engine._room_vents = {"r1": [RoomVent.create("r1", "cover.vent_r1")]}

        await engine._enter_min_runtime_hold(conn)

        engine._ha.open_cover.assert_not_awaited()
        await conn.close()
