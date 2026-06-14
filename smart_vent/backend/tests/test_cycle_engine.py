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

from backend.engine.cycle_engine import (
    CycleEngine,
    CycleState,
    _climate_temp_to_f,
    _effective_deadband,
    _is_at_target,
)
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
    deadband_override: float | None = None,
) -> Room:
    return Room(
        id=room_id,
        name=name,
        thermostat_entity_id=THERMO_ID,
        temp_offset=temp_offset,
        deadband_override=deadband_override,
    )


def _make_tc(**overrides: object) -> ThermostatConfig:
    """Build a ThermostatConfig for tests.

    Translates a legacy ``min_open_vents`` kwarg into the post-#213 fields so
    existing test setups keep their semantics:

    * ``0`` → ``has_bypass_damper=True`` (no airflow floor)
    * ``1`` → defaults (engine fallback returns 1 when ``total_vents_count`` is None)
    * ``N>1`` → ``total_vents_count=N, min_open_vents_fraction=1.0``
    """
    defaults: dict[str, object] = {
        "thermostat_entity_id": THERMO_ID,
        "overshoot_delta": 2.0,
        "deadband": 0.5,
        "min_setpoint": 60.0,
        "max_setpoint": 85.0,
    }
    legacy = overrides.pop("min_open_vents", None)
    if legacy == 0:
        defaults["has_bypass_damper"] = True
    elif isinstance(legacy, int) and legacy > 1:
        defaults["total_vents_count"] = legacy
        defaults["min_open_vents_fraction"] = 1.0
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
# Issue #296: idle setpoint-to-ambient reset is idempotent
# ---------------------------------------------------------------------------


class TestResetSetpointToAmbient:
    """_reset_setpoint_to_ambient must only call HA when the setpoint differs
    from ambient, so an idle house within deadband does not churn HA every tick."""

    @pytest.mark.asyncio
    async def test_skips_call_when_setpoint_already_at_ambient(self):
        ha = _make_ha(ambient=72.0)  # setpoint == current_temperature == 72
        engine = _make_engine(ha)
        await engine._reset_setpoint_to_ambient(ha.get_state.return_value)
        ha.set_thermostat_temperature.assert_not_called()
        # The tracked value is still kept in sync for the reconciler.
        assert engine._last_setpoint_sent == pytest.approx(72.0)

    @pytest.mark.asyncio
    async def test_sends_call_when_setpoint_differs_from_ambient(self):
        ha = _make_ha(ambient=72.0)
        ha.get_state.return_value["attributes"]["temperature"] = 68.0
        engine = _make_engine(ha)
        await engine._reset_setpoint_to_ambient(ha.get_state.return_value)
        ha.set_thermostat_temperature.assert_called_once()
        assert ha.set_thermostat_temperature.call_args.args[1] == pytest.approx(72.0)
        assert engine._last_setpoint_sent == pytest.approx(72.0)

    @pytest.mark.asyncio
    async def test_noop_when_ambient_unreadable(self):
        ha = _make_ha(ambient=72.0)
        ha.get_state.return_value["attributes"]["current_temperature"] = None
        engine = _make_engine(ha)
        await engine._reset_setpoint_to_ambient(ha.get_state.return_value)
        ha.set_thermostat_temperature.assert_not_called()


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
        def get_numeric(eid, max_age_min=None):
            return None

        ha.get_numeric_state.side_effect = get_numeric

        # Use thermostat ambient as proxy (72) for both rooms since no sensors
        # With ambient=72, target=74, deadband=0.5:
        # - effective=72 vs target-deadband=73.5 → 72 < 73.5 → needs heat → excluded
        # All rooms use the same ambient fallback so both would be excluded or kept
        # Let's set up with actual sensor map instead
        engine._sensor_map = {"r1": ["sensor.r1"], "r2": ["sensor.r2"]}

        def get_numeric_state(eid, max_age_min=None):
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

        def get_numeric_state(eid, max_age_min=None):
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
        ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: {
            "s1": 80.0,
            "s2": 79.0,
        }.get(eid)

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
        ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: {
            "s1": 65.0,
            "s2": 66.0,
        }.get(eid)

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
        ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: {
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
        ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: {
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
# Per-room deadband override (Issue #277)
# ---------------------------------------------------------------------------


class TestEffectiveDeadband:
    """The _effective_deadband helper resolves override vs inheritance."""

    def test_none_inherits_thermostat_deadband(self):
        room = _make_room("r1", deadband_override=None)
        assert _effective_deadband(room, 0.5) == 0.5

    def test_override_replaces_thermostat_deadband(self):
        room = _make_room("r1", deadband_override=2.0)
        assert _effective_deadband(room, 0.5) == 2.0

    def test_zero_override_is_honored_not_treated_as_unset(self):
        # 0.0 is a valid (exact-match) deadband and must not collapse to the
        # thermostat value — only None inherits.
        room = _make_room("r1", deadband_override=0.0)
        assert _effective_deadband(room, 0.5) == 0.0


class TestInferModeDeadbandOverride:
    """A room's deadband override governs its own start-cycle vote."""

    @pytest.mark.asyncio
    async def test_wide_override_suppresses_demand_within_band(self):
        # Room is 1.5°F above target. The thermostat's 0.5°F deadband would call
        # for cooling, but the room's 2.0°F override keeps it 'at target' → off.
        ha = _make_ha(ambient=74.0)
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["s1"]}
        ha.get_numeric_state.return_value = 75.5

        rooms = {
            "r1": ActiveRoom(
                room=_make_room("r1", deadband_override=2.0),
                target_temp=74.0,
                source="schedule",
            ),
        }
        thermo = ha.get_state(THERMO_ID)
        result = await engine._infer_mode_from_room_temps(rooms, 0.5, thermo)
        assert result == "off"

    @pytest.mark.asyncio
    async def test_narrow_override_calls_for_hvac_inside_thermostat_band(self):
        # Room is 0.3°F above target. The thermostat's 0.5°F deadband would treat
        # it as 'at target', but the room's 0.2°F override demands cooling.
        ha = _make_ha(ambient=74.0)
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["s1"]}
        ha.get_numeric_state.return_value = 74.3

        rooms = {
            "r1": ActiveRoom(
                room=_make_room("r1", deadband_override=0.2),
                target_temp=74.0,
                source="schedule",
            ),
        }
        thermo = ha.get_state(THERMO_ID)
        result = await engine._infer_mode_from_room_temps(rooms, 0.5, thermo)
        assert result == "cooling"

    @pytest.mark.asyncio
    async def test_filter_excludes_opposite_demand_via_override(self):
        # Heating cycle. Room is 0.3°F above target. Under the thermostat's 0.5°F
        # deadband it is 'at target' (vote off) and would ride the cycle; the
        # room's narrow 0.2°F override flips it to a cooling vote, so the filter
        # excludes it from the heating cycle (opposite direction).
        ha = _make_ha(ambient=74.0)
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["s1"]}
        ha.get_numeric_state.return_value = 74.3

        active = {
            "r1": ActiveRoom(
                room=_make_room("r1", deadband_override=0.2),
                target_temp=74.0,
                source="schedule",
            ),
        }
        thermo = ha.get_state(THERMO_ID)
        result = await engine._filter_rooms_for_mode(active, "heating", 0.5, thermo)
        assert "r1" not in result


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
        ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: {
            "s1": 70.0,
            "s2": 80.0,
        }.get(eid)

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
        ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: {
            "s1": 76.0,
            "s2": None,
        }.get(eid)

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

    def test_get_zone_status_reports_real_sensor_counts(self):
        """sensor_count / available_sensor_count reflect configured-vs-fresh
        sensors, not the old boolean-flag stand-in (Issue #270)."""
        ha = _make_ha(ambient=72.0)
        engine = _make_engine(ha)
        room = _make_room("r1")
        room.include_thermostat_sensor = True  # +1 sensor, fresh (ambient 72)
        engine._active_rooms = {
            "r1": ActiveRoom(room=room, target_temp=72.0, source="schedule"),
        }
        engine._sensor_map = {"r1": ["sensor.a", "sensor.b"]}
        # sensor.a fresh, sensor.b stale (returns None under the freshness guard).
        ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
            70.0 if eid == "sensor.a" else None
        )

        status = engine.get_zone_status()
        rs = status.rooms[0]
        # 2 configured sensors + the thermostat probe.
        assert rs.sensor_count == 3
        # sensor.a + the fresh thermostat probe; sensor.b is stale.
        assert rs.available_sensor_count == 2


class TestSensorCounts:
    """_sensor_counts — the (configured, available) pair behind RoomLiveState."""

    def test_no_sensors_no_thermostat(self):
        engine = _make_engine()
        engine._sensor_map = {"r1": []}
        room = _make_room("r1")
        assert engine._sensor_counts(room) == (0, 0)

    def test_all_fresh(self):
        ha = _make_ha()
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["s1", "s2"]}
        ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: 70.0
        assert engine._sensor_counts(_make_room("r1")) == (2, 2)

    def test_some_stale(self):
        ha = _make_ha()
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["s1", "s2", "s3"]}
        ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
            70.0 if eid == "s1" else None
        )
        assert engine._sensor_counts(_make_room("r1")) == (3, 1)

    def test_thermostat_sensor_counted_when_included(self):
        ha = _make_ha(ambient=72.0)
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": []}
        room = _make_room("r1")
        room.include_thermostat_sensor = True
        # No room sensors, but the thermostat probe is present and fresh.
        assert engine._sensor_counts(room) == (1, 1)

    def test_thermostat_sensor_counted_but_unavailable(self):
        ha = _make_ha()
        ha.get_state.return_value = None  # thermostat probe unreadable
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": []}
        room = _make_room("r1")
        room.include_thermostat_sensor = True
        # Configured (counts toward total) but not available.
        assert engine._sensor_counts(room) == (1, 0)


class TestMaybeBroadcast:
    """_maybe_broadcast — the WebSocket zone-status push (Issue #270)."""

    @pytest.mark.asyncio
    async def test_emits_zone_status_event(self):
        ha = _make_ha(ambient=72.0, hvac_mode="cool", hvac_action="idle")
        broadcast = AsyncMock()
        engine = CycleEngine(
            thermostat_entity_id=THERMO_ID,
            ha=ha,
            vent_ctrl=VentController(ha),
            broadcast=broadcast,
            get_enabled=lambda: True,
        )

        await engine._maybe_broadcast()

        broadcast.assert_awaited_once()
        event, payload = broadcast.await_args.args
        assert event == "zone_status"
        assert payload["thermostat_entity_id"] == THERMO_ID
        assert payload["cycle_state"] == "idle"

    @pytest.mark.asyncio
    async def test_noop_without_callback(self):
        engine = _make_engine()  # no broadcast configured
        await engine._maybe_broadcast()  # must not raise

    @pytest.mark.asyncio
    async def test_swallows_broadcast_errors(self):
        ha = _make_ha()
        broadcast = AsyncMock(side_effect=RuntimeError("ws gone"))
        engine = CycleEngine(
            thermostat_entity_id=THERMO_ID,
            ha=ha,
            vent_ctrl=VentController(ha),
            broadcast=broadcast,
            get_enabled=lambda: True,
        )
        # A dead WebSocket must never bubble out of the tick.
        await engine._maybe_broadcast()
        broadcast.assert_awaited_once()


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
        ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: readings.get(eid)
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

    @pytest.mark.asyncio
    async def test_persists_in_min_runtime_hold_flag(self):
        """Issue #237: the hold flag must be persisted on the cycle log so the
        next tick recognises we are in hold and skips the close-vent loop."""
        from backend import db
        from backend.models import RoomVent

        engine, conn, cycle = await _setup_engine_with_running_cycle()
        engine._room_vents = {"r1": [RoomVent.create("r1", "cover.vent_r1")]}
        assert engine._cycle_log is not None
        assert engine._cycle_log.in_min_runtime_hold is False

        await engine._enter_min_runtime_hold(conn)

        # In-memory flag
        assert engine._cycle_log.in_min_runtime_hold is True
        # Persisted to DB
        reloaded = await db.get_cycle_log(conn, cycle.id)
        assert reloaded is not None
        assert reloaded.in_min_runtime_hold is True
        await conn.close()


class TestMinRuntimeHoldGate:
    """Issue #237: while a cycle is in the min-runtime hold the per-room
    close-vent loop must be short-circuited, otherwise vents the hold has just
    re-opened are re-closed on the next tick (the original thrashing bug).

    Two distinct branches in _monitor_rooms protect this:
    - the hold-ENTRY branch (all rooms satisfied, runtime unsatisfied) enters
      the hold and returns before the close loop;
    - the hold GATE (in_min_runtime_hold already set) short-circuits the close
      loop on later ticks — the only protection once a room has drifted off
      target during the hold, because the entry branch no longer fires then.
    """

    @pytest.mark.asyncio
    async def test_hold_entry_branch_skips_close_loop(self):
        """All rooms satisfied + runtime unsatisfied → the hold-entry branch
        returns before the close loop runs."""
        from backend.models import RoomVent

        engine, conn, cycle = await _setup_engine_with_running_cycle()
        # Force the hold state directly: cycle is satisfied but min runtime
        # has not elapsed.
        engine._cycle_log.in_min_runtime_hold = True
        engine._room_vents = {"r1": [RoomVent.create("r1", "cover.vent_r1")]}
        rcs = RoomCycleState(cycle_id=cycle.id, room_id="r1", target_temp=74.0)
        engine._room_cycle_states = {"r1": rcs}
        # Active room reads at-or-below target (would normally close the vent).
        engine._sensor_map = {"r1": ["s_r1"]}
        engine._ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
            73.5 if eid == "s_r1" else None
        )

        tc = _make_tc(min_cycle_runtime_min=10)
        # Cycle just started a moment ago so runtime is NOT satisfied.
        engine._cycle_log.started_at = datetime.now(UTC) - timedelta(seconds=30)
        from backend import db as _db

        await _db.upsert_thermostat_config(conn, tc)

        engine._ha.close_cover.reset_mock()
        await engine._monitor_rooms(conn, "cooling")

        # The hold-entry branch must prevent any close calls.
        engine._ha.close_cover.assert_not_awaited()
        # Vent state untouched.
        assert rcs.vent_closed_at is None
        await conn.close()

    @pytest.mark.asyncio
    async def test_hold_gate_prevents_reclosing_when_a_room_drifts_off_target(self):
        """One room drifted OFF target during the hold → the entry branch
        cannot fire (not all rooms satisfied), so only the gate stops the
        close loop from re-closing the at-target room's just-reopened vent —
        the original #237 thrashing bug."""
        from backend import db as _db
        from backend.models import RoomVent

        engine, conn, cycle = await _setup_engine_with_running_cycle()
        engine._cycle_log.in_min_runtime_hold = True

        room2 = Room.create(name="Office", thermostat_entity_id=THERMO_ID)
        room2.id = "r2"
        await _db.upsert_room(conn, room2)
        engine._active_rooms["r2"] = ActiveRoom(room=room2, target_temp=74.0, source="schedule")
        engine._room_vents = {
            "r1": [RoomVent.create("r1", "cover.vent_r1")],
            "r2": [RoomVent.create("r2", "cover.vent_r2")],
        }
        # r1: at target with its vent re-opened by the hold (vent_closed_at is
        # None) — exactly what the close loop would re-close.
        # r2: drifted off target during the hold.
        engine._room_cycle_states = {
            "r1": RoomCycleState(cycle_id=cycle.id, room_id="r1", target_temp=74.0),
            "r2": RoomCycleState(cycle_id=cycle.id, room_id="r2", target_temp=74.0),
        }
        engine._sensor_map = {"r1": ["s_r1"], "r2": ["s_r2"]}
        engine._ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: {
            "s_r1": 73.5,  # at target → close loop would re-close its vent
            "s_r2": 76.0,  # off target → hold-entry branch cannot fire
        }.get(eid)

        tc = _make_tc(min_cycle_runtime_min=10)
        engine._cycle_log.started_at = datetime.now(UTC) - timedelta(seconds=30)
        await _db.upsert_thermostat_config(conn, tc)

        engine._ha.close_cover.reset_mock()
        await engine._monitor_rooms(conn, "cooling")

        # The gate must short-circuit the close loop entirely.
        engine._ha.close_cover.assert_not_awaited()
        assert engine._room_cycle_states["r1"].vent_closed_at is None
        # The cycle keeps running — the gate defers, it does not terminate.
        assert engine._state == CycleState.RUNNING
        await conn.close()

    @pytest.mark.asyncio
    async def test_hold_terminates_cycle_once_runtime_satisfied(self):
        from backend.models import RoomVent

        engine, conn, cycle = await _setup_engine_with_running_cycle()
        engine._cycle_log.in_min_runtime_hold = True
        engine._room_vents = {"r1": [RoomVent.create("r1", "cover.vent_r1")]}
        engine._room_cycle_states = {
            "r1": RoomCycleState(cycle_id=cycle.id, room_id="r1", target_temp=74.0)
        }
        engine._sensor_map = {"r1": ["s_r1"]}
        engine._ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
            73.5 if eid == "s_r1" else None
        )

        tc = _make_tc(min_cycle_runtime_min=10)
        # Cycle started 15 min ago so the hold has now satisfied min runtime.
        engine._cycle_log.started_at = datetime.now(UTC) - timedelta(minutes=15)
        from backend import db as _db

        await _db.upsert_thermostat_config(conn, tc)

        await engine._monitor_rooms(conn, "cooling")

        # Cycle should now be terminated (state IDLE, cycle_log cleared).
        assert engine._state == CycleState.IDLE
        assert engine._cycle_log is None
        await conn.close()


class TestOverflowDuringHold:
    """Issue #237: during the min-runtime hold, non-active rooms that can
    absorb the surplus conditioning have their vents opened by
    ``_apply_overflow_during_hold``."""

    @pytest.mark.asyncio
    async def test_overflow_opens_tier1_candidate_vent(self):
        from backend import db
        from backend.models import RoomVent

        engine, conn, cycle = await _setup_engine_with_running_cycle()
        # Add an idle (non-active) candidate room: Office.
        office = Room.create(name="Office", thermostat_entity_id=THERMO_ID)
        office.id = "r_office"
        office.system_wide_temp = 70.0
        await db.upsert_room(conn, office)
        office_vent = RoomVent.create("r_office", "cover.vent_office")
        await db.add_room_vent(conn, office_vent)
        # Engine still treats only r1 as active.
        engine._room_vents = {"r1": [RoomVent.create("r1", "cover.vent_r1")]}
        engine._room_cycle_states = {
            "r1": RoomCycleState(cycle_id=cycle.id, room_id="r1", target_temp=68.0)
        }
        engine._sensor_map = {"r_office": ["s_office"]}
        engine._ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
            75.0 if eid == "s_office" else None
        )

        tc = _make_tc(min_cycle_runtime_min=10)
        await db.upsert_thermostat_config(conn, tc)

        await engine._apply_overflow_during_hold(conn, "cooling", tc)

        opened = [c.args[0] for c in engine._ha.open_cover.await_args_list]
        assert "cover.vent_office" in opened
        assert "r_office" in engine._overflow_room_ids
        await conn.close()

    @pytest.mark.asyncio
    async def test_overflow_disabled_skips_open(self):
        from backend import db
        from backend.models import RoomVent

        engine, conn, cycle = await _setup_engine_with_running_cycle()
        office = Room.create(name="Office", thermostat_entity_id=THERMO_ID)
        office.id = "r_office"
        office.system_wide_temp = 70.0
        await db.upsert_room(conn, office)
        await db.add_room_vent(conn, RoomVent.create("r_office", "cover.vent_office"))
        engine._room_vents = {"r1": [RoomVent.create("r1", "cover.vent_r1")]}
        engine._room_cycle_states = {
            "r1": RoomCycleState(cycle_id=cycle.id, room_id="r1", target_temp=68.0)
        }
        engine._sensor_map = {"r_office": ["s_office"]}
        engine._ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
            75.0 if eid == "s_office" else None
        )

        tc = _make_tc(min_cycle_runtime_min=10, overflow_during_min_runtime=False)
        await db.upsert_thermostat_config(conn, tc)

        await engine._apply_overflow_during_hold(conn, "cooling", tc)

        engine._ha.open_cover.assert_not_awaited()
        assert engine._overflow_room_ids == set()
        await conn.close()

    @pytest.mark.asyncio
    async def test_overflow_closes_room_that_is_no_longer_a_candidate(self):
        """A room previously opened as overflow must be closed when it drifts
        past its setpoint or another room outranks it."""
        from backend import db
        from backend.models import RoomVent

        engine, conn, cycle = await _setup_engine_with_running_cycle()
        office = Room.create(name="Office", thermostat_entity_id=THERMO_ID)
        office.id = "r_office"
        office.system_wide_temp = 70.0
        await db.upsert_room(conn, office)
        await db.add_room_vent(conn, RoomVent.create("r_office", "cover.vent_office"))
        engine._room_vents = {"r1": [RoomVent.create("r1", "cover.vent_r1")]}
        engine._room_cycle_states = {
            "r1": RoomCycleState(cycle_id=cycle.id, room_id="r1", target_temp=68.0)
        }
        engine._sensor_map = {"r_office": ["s_office"]}
        # First tick: office at 75 — qualifies for Tier 1.
        engine._ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
            75.0 if eid == "s_office" else None
        )

        tc = _make_tc(min_cycle_runtime_min=10)
        await db.upsert_thermostat_config(conn, tc)
        await engine._apply_overflow_during_hold(conn, "cooling", tc)
        assert "r_office" in engine._overflow_room_ids

        # Next tick: office cooled below its opposite trigger — no longer qualifies.
        engine._ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
            65.0 if eid == "s_office" else None
        )
        engine._ha.close_cover.reset_mock()
        await engine._apply_overflow_during_hold(conn, "cooling", tc)

        # The overflow vent must be closed and removed from the tracking set.
        closed_args = [c.args[0] for c in engine._ha.close_cover.await_args_list]
        assert "cover.vent_office" in closed_args
        assert "r_office" not in engine._overflow_room_ids
        await conn.close()

    @pytest.mark.asyncio
    async def test_overflow_skipped_in_vacation_mode(self):
        from backend import db
        from backend.models import RoomVent

        engine, conn, cycle = await _setup_engine_with_running_cycle()
        engine._get_vacation_mode = lambda: True
        office = Room.create(name="Office", thermostat_entity_id=THERMO_ID)
        office.id = "r_office"
        office.system_wide_temp = 70.0
        await db.upsert_room(conn, office)
        await db.add_room_vent(conn, RoomVent.create("r_office", "cover.vent_office"))
        engine._room_vents = {"r1": [RoomVent.create("r1", "cover.vent_r1")]}
        engine._room_cycle_states = {
            "r1": RoomCycleState(cycle_id=cycle.id, room_id="r1", target_temp=68.0)
        }
        engine._sensor_map = {"r_office": ["s_office"]}
        engine._ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
            75.0 if eid == "s_office" else None
        )

        tc = _make_tc(min_cycle_runtime_min=10)
        await db.upsert_thermostat_config(conn, tc)

        await engine._apply_overflow_during_hold(conn, "cooling", tc)

        engine._ha.open_cover.assert_not_awaited()
        assert engine._overflow_room_ids == set()
        await conn.close()


class TestOverflowRoomDataPoints:
    """Issue #254: overflow rooms are recorded as room_cycle_states rows tagged
    role='overflow' with start/end temperatures, so the Logs page can show the
    rooms a min-runtime hold redirected into."""

    async def _setup_with_office(self, office_temp):
        from backend import db
        from backend.models import RoomVent

        engine, conn, cycle = await _setup_engine_with_running_cycle()
        office = Room.create(name="Office", thermostat_entity_id=THERMO_ID)
        office.id = "r_office"
        office.system_wide_temp = 70.0
        await db.upsert_room(conn, office)
        await db.add_room_vent(conn, RoomVent.create("r_office", "cover.vent_office"))
        engine._room_vents = {"r1": [RoomVent.create("r1", "cover.vent_r1")]}
        engine._room_cycle_states = {
            "r1": RoomCycleState(cycle_id=cycle.id, room_id="r1", target_temp=68.0)
        }
        engine._sensor_map = {"r_office": ["s_office"]}
        engine._ha.get_numeric_state.side_effect = lambda eid, max_age_min=None, t=office_temp: (
            t if eid == "s_office" else None
        )
        tc = _make_tc(min_cycle_runtime_min=10)
        await db.upsert_thermostat_config(conn, tc)
        return engine, conn, cycle, tc

    @pytest.mark.asyncio
    async def test_open_records_overflow_room_state(self):
        from backend import db

        engine, conn, cycle, tc = await self._setup_with_office(75.0)
        await engine._apply_overflow_during_hold(conn, "cooling", tc)

        states = {r.room_id: r for r in await db.get_room_cycle_states(conn, cycle.id)}
        assert "r_office" in states
        ov = states["r_office"]
        assert ov.role == "overflow"
        assert ov.temp_at_start == 75.0
        assert ov.temp_at_end is None  # still open
        # target_temp reflects the room's effective setpoint, not a cycle target.
        assert ov.target_temp == 70.0
        await conn.close()

    @pytest.mark.asyncio
    async def test_close_captures_end_temp(self):
        from backend import db

        engine, conn, cycle, tc = await self._setup_with_office(75.0)
        await engine._apply_overflow_during_hold(conn, "cooling", tc)

        # Office drifts below its opposite trigger → no longer a candidate.
        engine._ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
            65.0 if eid == "s_office" else None
        )
        await engine._apply_overflow_during_hold(conn, "cooling", tc)

        states = {r.room_id: r for r in await db.get_room_cycle_states(conn, cycle.id)}
        ov = states["r_office"]
        assert ov.temp_at_start == 75.0  # preserved from first open
        assert ov.temp_at_end == 65.0  # captured at vent-close
        assert ov.vent_closed_at is not None
        await conn.close()

    @pytest.mark.asyncio
    async def test_reopen_clears_end_temp(self):
        from backend import db

        engine, conn, cycle, tc = await self._setup_with_office(75.0)
        await engine._apply_overflow_during_hold(conn, "cooling", tc)
        # Swap out, then bring it back as a candidate on a later tick.
        engine._ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
            65.0 if eid == "s_office" else None
        )
        await engine._apply_overflow_during_hold(conn, "cooling", tc)
        engine._ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: (
            76.0 if eid == "s_office" else None
        )
        await engine._apply_overflow_during_hold(conn, "cooling", tc)

        states = {r.room_id: r for r in await db.get_room_cycle_states(conn, cycle.id)}
        ov = states["r_office"]
        assert ov.temp_at_start == 75.0  # first open value, never overwritten
        assert ov.temp_at_end is None  # cleared on re-open
        await conn.close()

    @pytest.mark.asyncio
    async def test_finalize_fills_end_temp_for_still_open_rooms(self):
        from backend import db

        engine, conn, cycle, tc = await self._setup_with_office(75.0)
        await engine._apply_overflow_during_hold(conn, "cooling", tc)
        assert "r_office" in engine._overflow_room_states

        await engine._finalize_overflow_rooms(conn)

        states = {r.room_id: r for r in await db.get_room_cycle_states(conn, cycle.id)}
        ov = states["r_office"]
        assert ov.temp_at_end == 75.0  # filled at cycle end from live temp
        assert ov.vent_closed_at is not None
        # In-memory tracking is cleared after finalize.
        assert engine._overflow_room_states == {}
        await conn.close()

    @pytest.mark.asyncio
    async def test_get_cycle_ids_with_overflow(self):
        from backend import db

        engine, conn, cycle, tc = await self._setup_with_office(75.0)
        # Before any overflow: empty.
        assert await db.get_cycle_ids_with_overflow(conn, [cycle.id]) == set()
        await engine._apply_overflow_during_hold(conn, "cooling", tc)
        assert await db.get_cycle_ids_with_overflow(conn, [cycle.id]) == {cycle.id}
        # Empty input short-circuits.
        assert await db.get_cycle_ids_with_overflow(conn, []) == set()
        await conn.close()


class TestCoolingLockoutState:
    """_cooling_lockout_state — outdoor-temperature cooling lockout (Issue #209)."""

    @pytest.mark.asyncio
    async def test_disabled_when_threshold_unset(self):
        engine = _make_engine()
        tc = _make_tc(cooling_lockout_below_f=None)
        state, temp = await engine._cooling_lockout_state(None, tc)
        assert state == "disabled"
        assert temp is None

    @pytest.mark.asyncio
    async def test_sensor_unavailable_when_no_outdoor_reading(self):
        engine = _make_engine()
        engine._read_outside_temp = AsyncMock(return_value=None)
        tc = _make_tc(cooling_lockout_below_f=55.0)
        state, temp = await engine._cooling_lockout_state(None, tc)
        assert state == "sensor_unavailable"
        assert temp is None

    @pytest.mark.asyncio
    async def test_locked_out_when_outdoor_below_threshold(self):
        engine = _make_engine()
        engine._read_outside_temp = AsyncMock(return_value=45.0)
        tc = _make_tc(cooling_lockout_below_f=55.0)
        state, temp = await engine._cooling_lockout_state(None, tc)
        assert state == "locked_out"
        assert temp == 45.0

    @pytest.mark.asyncio
    async def test_allowed_when_outdoor_above_threshold(self):
        engine = _make_engine()
        engine._read_outside_temp = AsyncMock(return_value=68.0)
        tc = _make_tc(cooling_lockout_below_f=55.0)
        state, temp = await engine._cooling_lockout_state(None, tc)
        assert state == "allowed"
        assert temp == 68.0

    @pytest.mark.asyncio
    async def test_boundary_at_threshold_is_allowed(self):
        engine = _make_engine()
        engine._read_outside_temp = AsyncMock(return_value=55.0)
        tc = _make_tc(cooling_lockout_below_f=55.0)
        # Exactly at the threshold is not "below" it — cooling is allowed.
        state, _temp = await engine._cooling_lockout_state(None, tc)
        assert state == "allowed"


class TestSensorStalenessGuard:
    """_get_avg_temp + _emit_sensor_freshness_warnings — Issue #211.

    A battery sensor that drops off the mesh keeps its last numeric state in
    HA. Without a freshness guard, the engine would average that stale value
    into the room temperature and confidently make the wrong control decision.
    These tests pin both the math (stale readings excluded from the average)
    and the user-visible signal (a warning event written once per episode).
    """

    def _make_room(self, room_id: str = "r1") -> Room:
        return _make_room(room_id, name="Bedroom")

    def test_get_avg_temp_excludes_stale_sensors(self):
        room = self._make_room()
        engine = _make_engine()
        engine._sensor_map = {room.id: ["sensor.fresh", "sensor.stale"]}

        # Mock the HAClient to honor max_age_min: a stale call returns None.
        def get_numeric(eid: str, max_age_min: float | None = None) -> float | None:
            if eid == "sensor.fresh":
                return 70.0
            return None  # stale → guard returns None

        engine._ha.get_numeric_state.side_effect = get_numeric

        avg = engine._get_avg_temp(room)
        # Only the fresh sensor counts toward the average.
        assert avg == 70.0

    def test_get_avg_temp_returns_none_when_all_sensors_stale(self):
        room = self._make_room()
        engine = _make_engine()
        engine._sensor_map = {room.id: ["sensor.a", "sensor.b"]}
        engine._ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: None
        # No thermostat ambient fallback for this room.
        room.include_thermostat_sensor = False

        assert engine._get_avg_temp(room) is None

    @pytest.mark.asyncio
    async def test_emit_warning_once_per_stale_episode(self):
        """Warns the first tick a sensor goes stale; silent on subsequent ticks
        until the sensor recovers."""
        room = self._make_room()
        engine = _make_engine()
        engine._sensor_map = {room.id: ["sensor.dead"]}
        engine._logger = MagicMock()
        engine._logger.log = AsyncMock()
        # 90 minutes stale — well past the 30-minute threshold.
        engine._ha.get_state_age_seconds = MagicMock(return_value=90 * 60)

        ar = ActiveRoom(room=room, source="schedule", target_temp=72.0)

        await engine._emit_sensor_freshness_warnings({room.id: ar})
        await engine._emit_sensor_freshness_warnings({room.id: ar})
        await engine._emit_sensor_freshness_warnings({room.id: ar})

        # Exactly one warning event despite three ticks against the same stale
        # sensor — the rate-limit is doing its job.
        warn_calls = [c for c in engine._logger.log.await_args_list if c.args[0] == "warning"]
        assert len(warn_calls) == 1
        msg = warn_calls[0].args[2]
        assert "sensor.dead" in msg
        # Message names the age so a technician can act on it.
        assert "90" in msg

    @pytest.mark.asyncio
    async def test_recovery_emits_info_and_rearms_warning(self):
        """When a stale sensor reports a fresh reading again, the engine emits a
        recovery info event AND clears its 'already warned' flag so a future
        stale episode warns again."""
        room = self._make_room()
        engine = _make_engine()
        engine._sensor_map = {room.id: ["sensor.flaky"]}
        engine._logger = MagicMock()
        engine._logger.log = AsyncMock()

        age_seq = iter([90 * 60, 30, 90 * 60])  # stale → fresh → stale again
        engine._ha.get_state_age_seconds = MagicMock(side_effect=lambda _eid: next(age_seq))
        ar = ActiveRoom(room=room, source="schedule", target_temp=72.0)

        await engine._emit_sensor_freshness_warnings({room.id: ar})  # stale → warn
        await engine._emit_sensor_freshness_warnings({room.id: ar})  # fresh → recovery
        await engine._emit_sensor_freshness_warnings({room.id: ar})  # stale → warn again

        levels = [c.args[0] for c in engine._logger.log.await_args_list]
        # The "info" recovery sits between the two warnings — proving both that
        # recovery is announced and that the warned-set was cleared.
        assert levels == ["warning", "info", "warning"]

    @pytest.mark.asyncio
    async def test_missing_entity_not_warned(self):
        """An entity that has never been seen in the state cache returns None
        from get_state_age_seconds and is silently skipped — only sensors HA
        knows about but is no longer hearing from are flagged."""
        room = self._make_room()
        engine = _make_engine()
        engine._sensor_map = {room.id: ["sensor.never_existed"]}
        engine._logger = MagicMock()
        engine._logger.log = AsyncMock()
        engine._ha.get_state_age_seconds = MagicMock(return_value=None)

        ar = ActiveRoom(room=room, source="schedule", target_temp=72.0)
        await engine._emit_sensor_freshness_warnings({room.id: ar})

        engine._logger.log.assert_not_awaited()


# ---------------------------------------------------------------------------
# _reconcile_state: drift detection + correction (RUNNING and IDLE)
# ---------------------------------------------------------------------------


def _state_router(states: dict[str, dict]):
    """Return a get_state callable backed by an entity→state dict."""

    def _get(entity_id: str):
        return states.get(entity_id)

    return _get


class TestReconcileState:
    """_reconcile_state verifies actual HA vent/thermostat state matches engine
    intent and corrects any external drift, logging each correction."""

    @pytest.mark.asyncio
    async def test_running_reopens_vent_closed_externally(self):
        from backend import db
        from backend.models import RoomVent

        engine, conn, cycle = await _setup_engine_with_running_cycle()
        engine._logger = MagicMock()
        engine._logger.log = AsyncMock()

        vent = RoomVent.create("r1", "cover.vent_r1")
        await db.add_room_vent(conn, vent)
        engine._room_vents = {"r1": [vent]}
        # Engine intends the vent OPEN (vent_closed_at is None), but HA reports closed.
        rcs = RoomCycleState(cycle_id=cycle.id, room_id="r1", target_temp=74.0)
        engine._room_cycle_states = {"r1": rcs}

        states = {
            "cover.vent_r1": {"state": "closed", "attributes": {}},
            THERMO_ID: {"state": "cool", "attributes": {"temperature": 72.0}},
        }
        engine._ha.get_state.side_effect = _state_router(states)

        tc = _make_tc()
        await engine._reconcile_state(conn, tc)

        engine._ha.open_cover.assert_awaited()
        # A drift warning was logged under the reconcile category.
        warn_calls = [c for c in engine._logger.log.await_args_list if c.args[0] == "warning"]
        assert any("re-opened" in c.args[2] for c in warn_calls)
        await conn.close()

    @pytest.mark.asyncio
    async def test_running_recloses_vent_opened_externally(self):
        from backend import db
        from backend.models import RoomVent

        engine, conn, cycle = await _setup_engine_with_running_cycle()
        engine._logger = MagicMock()
        engine._logger.log = AsyncMock()

        vent = RoomVent.create("r1", "cover.vent_r1")
        await db.add_room_vent(conn, vent)
        engine._room_vents = {"r1": [vent]}
        # Engine closed this vent (vent_closed_at set), but HA reports it open.
        rcs = RoomCycleState(cycle_id=cycle.id, room_id="r1", target_temp=74.0)
        rcs.vent_closed_at = datetime.now(UTC)
        engine._room_cycle_states = {"r1": rcs}

        states = {
            "cover.vent_r1": {"state": "open", "attributes": {}},
            THERMO_ID: {"state": "cool", "attributes": {"temperature": 72.0}},
        }
        engine._ha.get_state.side_effect = _state_router(states)
        # close_room_vents honours the airflow floor; bypass damper disables it
        # so the re-close actually dispatches.
        tc = _make_tc(min_open_vents=0)

        await engine._reconcile_state(conn, tc)

        engine._ha.close_cover.assert_awaited()
        await conn.close()

    @pytest.mark.asyncio
    async def test_running_reasserts_setpoint_drift(self):
        engine, conn, _cycle = await _setup_engine_with_running_cycle()
        engine._logger = MagicMock()
        engine._logger.log = AsyncMock()
        engine._room_vents = {}
        engine._room_cycle_states = {}
        engine._last_setpoint_sent = 70.0
        engine._cycle_ha_mode = "cool"

        # HA reports a setpoint that drifted away from _last_setpoint_sent.
        states = {THERMO_ID: {"state": "cool", "attributes": {"temperature": 75.0}}}
        engine._ha.get_state.side_effect = _state_router(states)

        await engine._reconcile_state(conn, _make_tc())

        engine._ha.set_thermostat_temperature.assert_awaited()
        args = engine._ha.set_thermostat_temperature.await_args
        # Re-asserts the engine's intended setpoint, not HA's drifted value.
        assert args.args[1] == 70.0
        await conn.close()

    @pytest.mark.asyncio
    async def test_running_reasserts_mode_drift(self):
        engine, conn, _cycle = await _setup_engine_with_running_cycle()
        engine._logger = MagicMock()
        engine._logger.log = AsyncMock()
        engine._room_vents = {}
        engine._room_cycle_states = {}
        engine._last_setpoint_sent = 70.0
        engine._cycle_ha_mode = "cool"

        # Thermostat was switched to heat_cool mid-cycle; setpoint unchanged.
        states = {THERMO_ID: {"state": "heat_cool", "attributes": {"temperature": 70.0}}}
        engine._ha.get_state.side_effect = _state_router(states)

        await engine._reconcile_state(conn, _make_tc())

        engine._ha.set_thermostat_temperature.assert_awaited()
        assert engine._ha.set_thermostat_temperature.await_args.kwargs["hvac_mode"] == "cool"
        await conn.close()

    @pytest.mark.asyncio
    async def test_running_no_correction_when_in_sync(self):
        engine, conn, _cycle = await _setup_engine_with_running_cycle()
        engine._logger = MagicMock()
        engine._logger.log = AsyncMock()
        engine._room_vents = {}
        engine._room_cycle_states = {}
        engine._last_setpoint_sent = 70.0
        engine._cycle_ha_mode = "cool"

        states = {THERMO_ID: {"state": "cool", "attributes": {"temperature": 70.0}}}
        engine._ha.get_state.side_effect = _state_router(states)

        await engine._reconcile_state(conn, _make_tc())

        engine._ha.set_thermostat_temperature.assert_not_awaited()
        await conn.close()

    @pytest.mark.asyncio
    async def test_idle_reopens_externally_closed_vent(self):
        from backend import db
        from backend.models import RoomVent

        engine, conn, _cycle = await _setup_engine_with_running_cycle()
        engine._state = CycleState.IDLE
        engine._logger = MagicMock()
        engine._logger.log = AsyncMock()

        vent = RoomVent.create("r1", "cover.vent_r1")
        await db.add_room_vent(conn, vent)

        # Idle: every zone vent should be open; HA reports one closed.
        states = {
            "cover.vent_r1": {"state": "closed", "attributes": {}},
            THERMO_ID: {"state": "cool", "attributes": {"temperature": 72.0}},
        }
        engine._ha.get_state.side_effect = _state_router(states)

        await engine._reconcile_state(conn, _make_tc())

        engine._ha.open_cover.assert_awaited()
        await conn.close()

    @pytest.mark.asyncio
    async def test_idle_warns_on_setpoint_outside_bounds(self):
        engine, conn, _cycle = await _setup_engine_with_running_cycle()
        engine._state = CycleState.IDLE
        engine._logger = MagicMock()
        engine._logger.log = AsyncMock()

        # Setpoint 90 is above max_setpoint=85 → should log a DB-settings drift warning.
        states = {THERMO_ID: {"state": "cool", "attributes": {"temperature": 90.0}}}
        engine._ha.get_state.side_effect = _state_router(states)

        await engine._reconcile_state(conn, _make_tc(max_setpoint=85.0))

        warn_calls = [c for c in engine._logger.log.await_args_list if c.args[0] == "warning"]
        assert any("outside configured bounds" in c.args[2] for c in warn_calls)
        await conn.close()


# ---------------------------------------------------------------------------
# restore_from_db: startup cycle resumption edge cases
# ---------------------------------------------------------------------------


class TestRestoreFromDb:
    """restore_from_db resumes an open cycle at startup, handling duplicates,
    deleted rooms, and a persisted mode that current ambient now contradicts."""

    async def _fresh_db(self):
        from backend import db

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)
        return conn

    @pytest.mark.asyncio
    async def test_no_open_logs_is_noop(self):
        conn = await self._fresh_db()
        engine = _make_engine()
        await engine.restore_from_db(conn)
        assert engine._state == CycleState.IDLE
        assert engine._cycle_log is None
        await conn.close()

    @pytest.mark.asyncio
    async def test_closes_duplicate_open_logs_and_restores_newest(self):
        from backend import db

        conn = await self._fresh_db()
        await db.upsert_thermostat_config(conn, _make_tc())
        room = Room.create(name="Test Room", thermostat_entity_id=THERMO_ID)
        room.id = "r1"
        await db.upsert_room(conn, room)

        # Two open cycle logs (the pre-fix duplicate-cycle bug).
        rooms_json = json.dumps({"r1": {"name": "Test Room", "target": 74.0, "source": "schedule"}})
        older = CycleLog.create(
            thermostat_entity_id=THERMO_ID, mode="cooling", rooms_json=rooms_json
        )
        newer = CycleLog.create(
            thermostat_entity_id=THERMO_ID, mode="cooling", rooms_json=rooms_json
        )
        await db.insert_cycle_log(conn, older)
        await db.insert_cycle_log(conn, newer)

        # Ambient consistent with cooling so the cycle is restored (not discarded).
        ha = _make_ha(ambient=80.0)
        engine = _make_engine(ha)

        await engine.restore_from_db(conn)

        assert engine._state == CycleState.RUNNING
        assert engine._cycle_log is not None
        # Exactly one open log remains.
        remaining = await db.get_open_cycle_logs(conn, THERMO_ID)
        assert len(remaining) == 1
        await conn.close()

    @pytest.mark.asyncio
    async def test_skips_deleted_rooms(self):
        from backend import db

        conn = await self._fresh_db()
        await db.upsert_thermostat_config(conn, _make_tc())
        room = Room.create(name="Real Room", thermostat_entity_id=THERMO_ID)
        room.id = "r1"
        await db.upsert_room(conn, room)

        # Snapshot references a room that no longer exists in the DB.
        rooms_json = json.dumps(
            {
                "r1": {"name": "Real Room", "target": 74.0, "source": "schedule"},
                "ghost": {"name": "Deleted", "target": 74.0, "source": "schedule"},
            }
        )
        cycle = CycleLog.create(
            thermostat_entity_id=THERMO_ID, mode="cooling", rooms_json=rooms_json
        )
        await db.insert_cycle_log(conn, cycle)

        ha = _make_ha(ambient=80.0)
        engine = _make_engine(ha)
        await engine.restore_from_db(conn)

        assert engine._state == CycleState.RUNNING
        assert "r1" in engine._active_rooms
        assert "ghost" not in engine._active_rooms
        await conn.close()

    @pytest.mark.asyncio
    async def test_discards_cycle_when_ambient_contradicts_mode(self):
        from backend import db

        conn = await self._fresh_db()
        await db.upsert_thermostat_config(conn, _make_tc())
        room = Room.create(name="Test Room", thermostat_entity_id=THERMO_ID)
        room.id = "r1"
        await db.upsert_room(conn, room)

        # Persisted a HEATING cycle targeting 74, but ambient is now 80 (>target):
        # the space no longer needs heat, so the stale cycle must be discarded.
        rooms_json = json.dumps({"r1": {"name": "Test Room", "target": 74.0, "source": "schedule"}})
        cycle = CycleLog.create(
            thermostat_entity_id=THERMO_ID, mode="heating", rooms_json=rooms_json
        )
        await db.insert_cycle_log(conn, cycle)

        ha = _make_ha(ambient=80.0)
        engine = _make_engine(ha)
        await engine.restore_from_db(conn)

        # Engine stays IDLE; state was reset.
        assert engine._state == CycleState.IDLE
        assert engine._cycle_log is None
        assert engine._active_rooms == {}
        # The stale log was closed in the DB.
        remaining = await db.get_open_cycle_logs(conn, THERMO_ID)
        assert remaining == []
        await conn.close()


# ---------------------------------------------------------------------------
# Ambient-aware presence suppression / pre-cool (Issue #248, Phase 2)
# ---------------------------------------------------------------------------


def _supp_room(
    *,
    room_id: str = "r1",
    enabled: bool = True,
    mode: str = "any_presence",
    min_differential: float = 5.0,
    deadband: float = 3.0,
) -> Room:
    return Room(
        id=room_id,
        name="Office",
        thermostat_entity_id=THERMO_ID,
        ambient_suppression_enabled=enabled,
        ambient_suppression_mode=mode,
        ambient_suppression_min_differential=min_differential,
        ambient_suppression_deadband=deadband,
    )


class TestSuppressionVote:
    """The per-room demand vote with ambient pre-cool/pre-heat (Issue #248).

    Fixed scenario unless noted: target 70°F, normal deadband 2°F, widened
    deadband 3°F, min differential 5°F, thermostat min/max setpoint 60/85°F.
    """

    def _vote(
        self,
        engine,
        room,
        effective,
        *,
        outside,
        target=70.0,
        source="presence",
        tc=None,
        recently_off=False,
    ):
        if tc is None:
            tc = _make_tc(deadband=2.0, min_setpoint=60.0, max_setpoint=85.0)
        return engine._suppression_vote(
            room, effective, target, source, 2.0, outside, tc, recently_off
        )

    def test_coast_up_suppresses_heat(self):
        # 67°F room, 80°F out: at the widened floor (70-3=67) -> hold off.
        engine = _make_engine()
        assert self._vote(engine, _supp_room(), 67.0, outside=80.0) == ("off", True)

    def test_coast_up_below_widened_floor_still_heats(self):
        # 66°F < widened floor 67°F -> comfort protection heats, not suppressed.
        engine = _make_engine()
        assert self._vote(engine, _supp_room(), 66.0, outside=80.0) == ("heat", False)

    def test_threshold_crossing_cools_at_normal_not_widened(self):
        # Coasting up, the room overshoots past target. Even at 72.9°F (inside
        # the widened ceiling 73°F) it must cool, because crossing the target
        # reverts the cool side to the NORMAL deadband (cool at 70+2=72, not 73).
        engine = _make_engine()
        assert self._vote(engine, _supp_room(), 72.1, outside=80.0) == ("cool", False)
        assert self._vote(engine, _supp_room(), 72.9, outside=80.0) == ("cool", False)

    def test_insufficient_differential_runs_heat(self):
        # 71°F outside is only +1 over target (< 5 differential) -> too little
        # push, so normal heating runs instead of coasting.
        engine = _make_engine()
        assert self._vote(engine, _supp_room(), 67.0, outside=71.0) == ("heat", False)

    def test_coast_down_suppresses_cool(self):
        # 72.5°F room (past the normal cool edge 72°F), 60°F out (<= 70-5):
        # coast down, widened ceiling 73°F -> hold off.
        engine = _make_engine()
        assert self._vote(engine, _supp_room(), 72.5, outside=60.0) == ("off", True)

    def test_coast_down_above_widened_ceiling_still_cools(self):
        engine = _make_engine()
        assert self._vote(engine, _supp_room(), 74.0, outside=60.0) == ("cool", False)

    def test_hard_cap_min_setpoint_overrides_suppression(self):
        # Widened floor 67°F would suppress a 67.5°F room, but a 68°F min setpoint
        # forces heat — the absolute comfort floor always wins.
        engine = _make_engine()
        tc = _make_tc(deadband=2.0, min_setpoint=68.0, max_setpoint=85.0)
        assert self._vote(engine, _supp_room(), 67.5, outside=80.0, tc=tc) == ("heat", False)

    def test_hard_cap_max_setpoint_overrides_suppression(self):
        engine = _make_engine()
        tc = _make_tc(deadband=2.0, min_setpoint=60.0, max_setpoint=72.0)
        assert self._vote(engine, _supp_room(), 72.5, outside=60.0, tc=tc) == ("cool", False)

    def test_disabled_room_uses_normal_vote(self):
        engine = _make_engine()
        assert self._vote(engine, _supp_room(enabled=False), 67.0, outside=80.0) == ("heat", False)

    def test_hard_cap_not_applied_when_feature_disabled(self):
        # The hard cap is part of the feature: a room not using pre-cool/pre-heat
        # keeps its plain deadband vote even at the setpoint bound. Target 60 ==
        # min_setpoint, room exactly at 60 -> within deadband -> "off", NOT a
        # hard-capped "heat".
        engine = _make_engine()
        tc = _make_tc(deadband=2.0, min_setpoint=60.0, max_setpoint=85.0)
        assert self._vote(
            engine, _supp_room(enabled=False), 60.0, outside=80.0, target=60.0, tc=tc
        ) == ("off", False)

    def test_non_presence_source_is_never_suppressed(self):
        engine = _make_engine()
        assert self._vote(engine, _supp_room(), 67.0, outside=80.0, source="schedule") == (
            "heat",
            False,
        )

    def test_off_schedule_mode_inert_without_recent_schedule(self):
        # off_schedule_only + not recently off a schedule -> normal heat.
        engine = _make_engine()
        assert self._vote(
            engine, _supp_room(mode="off_schedule_only"), 67.0, outside=80.0, recently_off=False
        ) == ("heat", False)

    def test_off_schedule_mode_engages_when_recently_off_schedule(self):
        # off_schedule_only + within the post-schedule window -> coast (suppress).
        engine = _make_engine()
        assert self._vote(
            engine, _supp_room(mode="off_schedule_only"), 67.0, outside=80.0, recently_off=True
        ) == ("off", True)

    def test_any_presence_ignores_off_schedule_flag(self):
        # any_presence engages regardless of the off-schedule flag.
        engine = _make_engine()
        assert self._vote(
            engine, _supp_room(mode="any_presence"), 67.0, outside=80.0, recently_off=False
        ) == ("off", True)

    def test_no_outside_reading_uses_normal_vote(self):
        engine = _make_engine()
        assert self._vote(engine, _supp_room(), 67.0, outside=None) == ("heat", False)


class TestSuppressionInInference:
    """End-to-end behavior through _infer_mode_from_room_temps / _filter."""

    def _tc(self):
        return _make_tc(deadband=2.0, min_setpoint=60.0, max_setpoint=85.0)

    @pytest.mark.asyncio
    async def test_all_rooms_suppressed_infers_off(self):
        # Two presence rooms coasting up on warm outside air -> no demand at all.
        ha = _make_ha(ambient=72.0)
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["s1"], "r2": ["s2"]}
        ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: {
            "s1": 67.0,
            "s2": 67.0,
        }.get(eid)
        rooms = {
            "r1": ActiveRoom(room=_supp_room(room_id="r1"), target_temp=70.0, source="presence"),
            "r2": ActiveRoom(room=_supp_room(room_id="r2"), target_temp=70.0, source="presence"),
        }
        result = await engine._infer_mode_from_room_temps(
            rooms, 2.0, ha.get_state(THERMO_ID), outside_temp=80.0, tc=self._tc()
        )
        assert result == "off"

    @pytest.mark.asyncio
    async def test_suppressed_room_excluded_from_another_rooms_heating_cycle(self):
        # Room A (presence) is coasting up and would normally call for heat —
        # the SAME direction as the cycle — but must still be excluded so it
        # coasts. Room B (schedule) genuinely needs heat and is kept.
        ha = _make_ha(ambient=68.0)
        engine = _make_engine(ha)
        engine._sensor_map = {"A": ["sa"], "B": ["sb"]}
        ha.get_numeric_state.side_effect = lambda eid, max_age_min=None: {
            "sa": 67.0,
            "sb": 68.0,
        }.get(eid)
        active = {
            "A": ActiveRoom(room=_supp_room(room_id="A"), target_temp=70.0, source="presence"),
            "B": ActiveRoom(room=_make_room("B"), target_temp=74.0, source="schedule"),
        }
        filtered = await engine._filter_rooms_for_mode(
            active, "heating", 2.0, ha.get_state(THERMO_ID), 80.0, self._tc()
        )
        assert set(filtered) == {"B"}

    @pytest.mark.asyncio
    async def test_insufficient_differential_room_still_drives_cycle(self):
        # Only +1°F outside -> no coasting -> the presence room votes heat.
        ha = _make_ha(ambient=68.0)
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["s1"]}
        ha.get_numeric_state.return_value = 67.0
        rooms = {
            "r1": ActiveRoom(room=_supp_room(room_id="r1"), target_temp=70.0, source="presence"),
        }
        result = await engine._infer_mode_from_room_temps(
            rooms, 2.0, ha.get_state(THERMO_ID), outside_temp=71.0, tc=self._tc()
        )
        assert result == "heating"

    @pytest.mark.asyncio
    async def test_compute_off_schedule_flags_respects_window(self):
        # Naive-local datetimes keep the result timezone-independent.
        from datetime import time

        from backend import db
        from backend.models import Schedule

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)

        room = _supp_room(room_id="r1", mode="off_schedule_only")
        room.ambient_suppression_off_schedule_window_min = 60
        await db.upsert_room(conn, room)
        await db.upsert_schedule(
            conn,
            Schedule(
                id="s1",
                room_id="r1",
                days_of_week=[0],  # Monday 22:00 -> Tuesday 07:00 (overnight)
                start_time=time(22, 0),
                end_time=time(7, 0),
                target_temp=68.0,
            ),
        )
        active = {"r1": ActiveRoom(room=room, target_temp=70.0, source="presence")}
        engine = _make_engine()

        within = await engine._compute_off_schedule_flags(
            conn,
            active,
            now=datetime(2026, 4, 14, 7, 30),  # 30 min after end
        )
        assert within == {"r1": True}

        outside = await engine._compute_off_schedule_flags(
            conn,
            active,
            now=datetime(2026, 4, 14, 9, 0),  # 120 min after end
        )
        assert outside == {"r1": False}

        await conn.close()

    @pytest.mark.asyncio
    async def test_compute_off_schedule_flags_skips_other_modes(self):
        # any_presence and disabled rooms are not queried at all.
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        from backend import db

        await db.init_db(conn)
        any_room = _supp_room(room_id="r1", mode="any_presence")
        active = {"r1": ActiveRoom(room=any_room, target_temp=70.0, source="presence")}
        engine = _make_engine()
        flags = await engine._compute_off_schedule_flags(conn, active)
        assert flags == {}
        await conn.close()


# ---------------------------------------------------------------------------
# Thermostat unavailability during a cycle (Issue #267)
# ---------------------------------------------------------------------------


class TestThermostatUnavailableAbort:
    """A transient thermostat outage must not kill a running cycle, but a
    sustained one must abort it: while the thermostat is unavailable _do_tick
    returns before the cycle timeout, the max_vent_closed_min watchdog, and
    reconciliation, so an open-ended outage would leave the physical HVAC
    running at the last commanded setpoint with vents closed and every safety
    monitor suspended. The threshold is the per-thermostat
    ``unavailable_abort_after_min`` config field (default 5 min, 0 = never)."""

    @pytest.mark.asyncio
    async def test_transient_outage_keeps_cycle_running(self):
        from backend import db

        engine, conn, _cycle = await _setup_engine_with_running_cycle()
        engine._ha.get_state.return_value = None

        # A couple of ticks well inside the 5-minute default threshold.
        await engine._do_tick(conn)
        await engine._do_tick(conn)

        assert engine.cycle_state == CycleState.RUNNING
        assert engine.unavailable_since is not None
        open_logs = await db.get_open_cycle_logs(conn, THERMO_ID)
        assert len(open_logs) == 1, "a transient outage must not abort the cycle"
        await conn.close()

    @pytest.mark.asyncio
    async def test_sustained_outage_aborts_cycle_and_reopens_vents(self):
        from backend import db
        from backend.models import RoomVent

        engine, conn, cycle = await _setup_engine_with_running_cycle()
        # Thermostat gone; the vent entity is still reachable (covers are
        # independent of the climate entity).
        engine._ha.get_state.side_effect = lambda eid: (
            None if eid == THERMO_ID else {"state": "closed", "attributes": {}}
        )
        engine._room_vents = {"r1": [RoomVent.create("r1", "cover.vent_r1")]}

        # Outage started 6 minutes ago — past the 5-minute default threshold.
        engine._unavailable_since = datetime.now(UTC) - timedelta(minutes=6)
        await engine._do_tick(conn)

        assert engine.cycle_state == CycleState.IDLE
        db_cycle = await db.get_cycle_log(conn, cycle.id)
        assert db_cycle is not None
        assert db_cycle.ended_reason == "aborted: thermostat unavailable"
        opened = [c.args[0] for c in engine._ha.open_cover.await_args_list]
        assert "cover.vent_r1" in opened, "abort must re-open the zone vents"
        await conn.close()

    @pytest.mark.asyncio
    async def test_zero_threshold_disables_the_abort(self):
        from backend import db

        engine, conn, _cycle = await _setup_engine_with_running_cycle()
        await db.upsert_thermostat_config(conn, _make_tc(unavailable_abort_after_min=0))
        engine._ha.get_state.return_value = None
        # Outage of an hour — with the guard disabled the cycle must survive.
        engine._unavailable_since = datetime.now(UTC) - timedelta(minutes=60)

        await engine._do_tick(conn)

        assert engine.cycle_state == CycleState.RUNNING
        open_logs = await db.get_open_cycle_logs(conn, THERMO_ID)
        assert len(open_logs) == 1
        await conn.close()

    @pytest.mark.asyncio
    async def test_recovery_clears_unavailable_since(self):
        engine, conn, _cycle = await _setup_engine_with_running_cycle()
        available_state = engine._ha.get_state.return_value

        engine._ha.get_state.return_value = None
        await engine._do_tick(conn)
        assert engine.unavailable_since is not None

        # Thermostat comes back — one available tick clears the outage clock,
        # so a later outage starts the threshold from zero again.
        engine._ha.get_state.return_value = available_state
        await engine._do_tick(conn)
        assert engine.unavailable_since is None
        await conn.close()

    @pytest.mark.asyncio
    async def test_idle_engine_skips_unavailable_ticks_without_abort(self):
        from backend import db

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)

        engine = _make_engine()
        engine._ha.get_state.return_value = None
        # Long-stale outage clock — an IDLE engine has nothing to abort.
        engine._unavailable_since = datetime.now(UTC) - timedelta(minutes=60)

        await engine._do_tick(conn)
        await engine._do_tick(conn)

        assert engine.cycle_state == CycleState.IDLE
        assert engine._cycle_log is None
        await conn.close()

    @pytest.mark.asyncio
    async def test_warns_once_per_outage_then_announces_recovery(self):
        """The event log gets one warning per outage episode (not one per
        60-second tick) and one recovery info event — mirroring the #211
        sensor-staleness rate-limiting so a long outage doesn't bury the feed
        (Issue #270)."""
        from backend import db

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)

        engine = _make_engine()
        engine._logger = MagicMock()
        engine._logger.log = AsyncMock()
        available_state = engine._ha.get_state.return_value

        # Three consecutive unavailable ticks (all inside the 5-min default).
        engine._ha.get_state.return_value = None
        for _ in range(3):
            await engine._do_tick(conn)
        # Recovery.
        engine._ha.get_state.return_value = available_state
        await engine._do_tick(conn)

        warn = [
            c
            for c in engine._logger.log.await_args_list
            if c.args[0] == "warning" and "unavailable" in c.args[2].lower()
        ]
        info = [
            c
            for c in engine._logger.log.await_args_list
            if c.args[0] == "info" and "reporting again" in c.args[2].lower()
        ]
        assert len(warn) == 1, "exactly one unavailability warning per outage episode"
        assert len(info) == 1, "exactly one recovery event"
        await conn.close()

    @pytest.mark.asyncio
    async def test_do_tick_skips_when_hvac_mode_unknown(self):
        """If the thermostat mode cannot be determined after active rooms are
        resolved, the tick bails without starting a cycle or commanding HVAC
        (defensive guard, Issue #270)."""
        from datetime import time as _time

        from backend import db
        from backend.models import Schedule

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)

        room = Room.create(name="Bedroom", thermostat_entity_id=THERMO_ID)
        room.id = "r1"
        await db.upsert_room(conn, room)
        # All-day schedule so the room resolves as active and the tick reaches
        # the mode check rather than returning earlier for "no active rooms".
        sched = Schedule.create(
            room_id="r1",
            days_of_week=list(range(7)),
            start_time=_time(0, 0),
            end_time=_time(23, 59),
            target_temp=72.0,
        )
        await db.upsert_schedule(conn, sched)

        ha = _make_ha(ambient=72.0)
        engine = _make_engine(ha)
        # Thermostat is reachable (passes the availability guard) but its mode
        # reads back indeterminate on this tick.
        engine._read_hvac_mode = lambda: "unknown"

        await engine._do_tick(conn)

        assert engine.cycle_state == CycleState.IDLE
        ha.set_thermostat_temperature.assert_not_called()
        await conn.close()


# ---------------------------------------------------------------------------
# In-tick abort guards: system disabled / vacation mode (Issue #269)
# ---------------------------------------------------------------------------


class TestDoTickAbortGuards:
    """The scheduler calls force_abort on toggles, but _do_tick carries its own
    guards as the backstop when a flag flips between scheduler events. If
    either guard regresses, a disabled system (or a vacation-mode house) keeps
    actively conditioning."""

    @pytest.mark.asyncio
    async def test_system_disabled_mid_cycle_aborts_on_tick(self):
        from backend import db

        engine, conn, cycle = await _setup_engine_with_running_cycle()
        engine._get_enabled = lambda: False

        await engine._do_tick(conn)

        assert engine.cycle_state == CycleState.IDLE
        db_cycle = await db.get_cycle_log(conn, cycle.id)
        assert db_cycle is not None
        assert db_cycle.ended_reason == "aborted: system disabled"
        await conn.close()

    @pytest.mark.asyncio
    async def test_vacation_mode_mid_cycle_aborts_and_applies_hold(self):
        from backend import db

        engine, conn, cycle = await _setup_engine_with_running_cycle()
        engine._get_vacation_mode = lambda: True
        engine._ha.set_thermostat_hvac_mode = AsyncMock()
        # Default config: vacation_hvac_mode="single", bounds 60–85. Ambient 72
        # is inside the band, so the hold turns the (still "cool") HVAC off.
        await db.upsert_thermostat_config(conn, _make_tc())

        await engine._do_tick(conn)

        assert engine.cycle_state == CycleState.IDLE
        db_cycle = await db.get_cycle_log(conn, cycle.id)
        assert db_cycle is not None
        assert db_cycle.ended_reason == "aborted: vacation mode"
        # The vacation hold ran in the same tick.
        engine._ha.set_thermostat_hvac_mode.assert_awaited_once_with(THERMO_ID, "off")
        await conn.close()


# ---------------------------------------------------------------------------
# Issue #280: climate-entity temperatures are reported/accepted in HA's system
# unit; the engine must normalise them to its internal °F. These exercise a
# *raw* °C current_temperature rather than pre-converting it in the mock.
# ---------------------------------------------------------------------------


class TestClimateTempToF:
    def test_none_returns_none(self):
        assert _climate_temp_to_f(None, "C") is None

    def test_unparseable_returns_none(self):
        assert _climate_temp_to_f("unavailable", "C") is None

    def test_fahrenheit_is_identity(self):
        assert _climate_temp_to_f(69.8, "F") == 69.8

    def test_celsius_converts_to_f(self):
        assert _climate_temp_to_f(21.0, "C") == 69.8

    def test_non_c_unit_treated_as_fahrenheit(self):
        # A test double / unexpected value that is not the string "C" must not
        # convert — protects MagicMock-based engine tests where ha_temp_unit is
        # auto-created and never equals "C".
        assert _climate_temp_to_f(70.0, MagicMock()) == 70.0


class TestMetricClimateReads:
    def test_read_thermo_temp_normalises_celsius_to_f(self):
        ha = _make_ha(ambient=21.0)  # thermostat reports 21 in HA's native unit
        ha.ha_temp_unit = "C"
        engine = _make_engine(ha)
        cur, sp = engine._read_thermo_temp_and_setpoint()
        assert cur == pytest.approx(69.8, abs=0.05)
        assert sp == pytest.approx(69.8, abs=0.05)

    def test_thermostat_probe_average_consistent_with_celsius(self):
        # include_thermostat_sensor mixes the thermostat probe with room sensors.
        # On a metric HA the probe is °C; the sensor (via get_numeric_state) is
        # already °F. The average must be taken in a single unit (°F).
        ha = _make_ha(ambient=20.0)  # probe reads 20°C → 68°F
        ha.ha_temp_unit = "C"
        ha.get_numeric_state.return_value = 68.0  # a room sensor already in °F
        engine = _make_engine(ha)
        room = _make_room()
        room.include_thermostat_sensor = True
        engine._sensor_map = {room.id: ["sensor.bedroom"]}
        avg = engine._get_avg_temp(room)
        # Both contributions are 68°F → average 68°F, not a °C/°F mash-up.
        assert avg == pytest.approx(68.0, abs=0.05)
