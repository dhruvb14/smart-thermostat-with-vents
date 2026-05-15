"""
Tests for vacation mode.

Covers:
  - Scheduler: load/persist vacation mode, auto-expiry on tick
  - Cycle engine: vacation mode guard (abort + hold), range vs single-setpoint
  - Cycle engine: HVAC resumes correctly after vacation ends
  - API endpoints: GET/POST/DELETE /api/settings/vacation-mode
  - Thermostat PUT: vacation_hvac_mode field persisted
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from backend import db
from backend.engine.cycle_engine import CycleEngine
from backend.engine.vent_controller import VentController
from backend.models import PresenceHoldoverState, Room, ThermostatConfig
from backend.scheduler import Scheduler

# ---------------------------------------------------------------------------
# Helpers (shared with test_scheduler.py pattern)
# ---------------------------------------------------------------------------

THERMO_A = "climate.thermo_a"


def _make_ha(hvac_mode: str = "heat", current_temp: float = 72.0) -> MagicMock:
    ha = MagicMock()
    ha.subscribe_all = MagicMock()
    ha.get_state.return_value = {
        "state": hvac_mode,
        "attributes": {
            "current_temperature": current_temp,
            "temperature": 72.0,
            "hvac_action": "idle",
        },
    }
    ha.get_numeric_state.return_value = None
    ha.set_thermostat_temperature = AsyncMock()
    ha.set_thermostat_hvac_mode = AsyncMock()
    ha.set_thermostat_temperature_range = AsyncMock()
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


async def _insert_room(conn: aiosqlite.Connection, room_id: str, thermo: str) -> Room:
    room = Room(id=room_id, name="Room", thermostat_entity_id=thermo)
    await db.upsert_room(conn, room)
    return room


# ---------------------------------------------------------------------------
# DB migration: vacation_hvac_mode column
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thermostat_config_vacation_hvac_mode_default():
    conn = await _setup_db()
    try:
        tc = ThermostatConfig(thermostat_entity_id=THERMO_A)
        await db.upsert_thermostat_config(conn, tc)
        loaded = await db.get_thermostat_config(conn, THERMO_A)
        assert loaded.vacation_hvac_mode == "single"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_thermostat_config_vacation_hvac_mode_range():
    conn = await _setup_db()
    try:
        tc = ThermostatConfig(thermostat_entity_id=THERMO_A, vacation_hvac_mode="range")
        await db.upsert_thermostat_config(conn, tc)
        loaded = await db.get_thermostat_config(conn, THERMO_A)
        assert loaded.vacation_hvac_mode == "range"
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Scheduler: vacation mode persistence and auto-expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_vacation_mode_set_and_get():
    ha = _make_ha()
    sched = Scheduler(ha=ha, db_path=":memory:")
    sched._db_conn = await _setup_db()
    sched._broadcast = None
    sched._event_logger = None

    assert not sched.get_vacation_mode()
    return_at = datetime.now(UTC) + timedelta(days=7)
    await sched.set_vacation_mode(True, return_at)
    assert sched.get_vacation_mode()
    assert sched.get_vacation_return_at() == return_at

    # Persisted: reload from DB
    val = await db.get_system_setting(sched._db_conn, "vacation_mode_enabled", "0")
    assert val == "1"

    await sched._db_conn.close()


@pytest.mark.asyncio
async def test_scheduler_vacation_mode_disable():
    ha = _make_ha()
    sched = Scheduler(ha=ha, db_path=":memory:")
    sched._db_conn = await _setup_db()
    sched._broadcast = None
    sched._event_logger = None

    return_at = datetime.now(UTC) + timedelta(days=7)
    await sched.set_vacation_mode(True, return_at)
    await sched.set_vacation_mode(False)

    assert not sched.get_vacation_mode()
    assert sched.get_vacation_return_at() is None
    val = await db.get_system_setting(sched._db_conn, "vacation_mode_enabled", "0")
    assert val == "0"

    await sched._db_conn.close()


@pytest.mark.asyncio
async def test_scheduler_vacation_mode_auto_expiry():
    """Expiry in the past → _check_vacation_expiry clears vacation mode."""
    ha = _make_ha()
    sched = Scheduler(ha=ha, db_path=":memory:")
    sched._db_conn = await _setup_db()
    sched._broadcast = None
    sched._event_logger = None
    sched._engines = {}

    # Set a return_at in the past
    past = datetime.now(UTC) - timedelta(seconds=1)
    sched._vacation_mode = True
    sched._vacation_return_at = past
    await db.set_system_setting(sched._db_conn, "vacation_mode_enabled", "1")
    await db.set_system_setting(sched._db_conn, "vacation_mode_return_at", past.isoformat())

    await sched._check_vacation_expiry()

    assert not sched.get_vacation_mode()
    assert sched.get_vacation_return_at() is None

    await sched._db_conn.close()


@pytest.mark.asyncio
async def test_scheduler_vacation_mode_not_expired_yet():
    """Future return_at → expiry check does NOT disable vacation mode."""
    ha = _make_ha()
    sched = Scheduler(ha=ha, db_path=":memory:")
    sched._db_conn = await _setup_db()
    sched._broadcast = None
    sched._event_logger = None
    sched._engines = {}

    future = datetime.now(UTC) + timedelta(hours=48)
    sched._vacation_mode = True
    sched._vacation_return_at = future

    await sched._check_vacation_expiry()
    assert sched.get_vacation_mode()

    await sched._db_conn.close()


# ---------------------------------------------------------------------------
# Cycle engine: vacation mode guard
# ---------------------------------------------------------------------------


def _make_engine(
    ha: MagicMock,
    vacation_mode: bool = False,
    system_enabled: bool = True,
) -> CycleEngine:
    vent_ctrl = MagicMock()
    vent_ctrl.open_all = AsyncMock()
    vent_ctrl.get_vent_states = MagicMock(return_value={})
    return CycleEngine(
        thermostat_entity_id=THERMO_A,
        ha=ha,
        vent_ctrl=vent_ctrl,
        get_enabled=lambda: system_enabled,
        get_vacation_mode=lambda: vacation_mode,
    )


@pytest.mark.asyncio
async def test_engine_vacation_mode_range_sets_heat_cool():
    """Range mode: set_thermostat_temperature_range called with min/max."""
    ha = _make_ha(hvac_mode="heat", current_temp=72.0)
    engine = _make_engine(ha, vacation_mode=True)

    conn = await _setup_db()
    try:
        await _insert_room(conn, "room1", THERMO_A)
        tc = ThermostatConfig(
            thermostat_entity_id=THERMO_A,
            min_setpoint=62.0,
            max_setpoint=80.0,
            vacation_hvac_mode="range",
        )
        await db.upsert_thermostat_config(conn, tc)

        await engine.tick(conn)

        ha.set_thermostat_temperature_range.assert_called_once_with(THERMO_A, 62.0, 80.0)
        ha.set_thermostat_hvac_mode.assert_not_called()
        ha.set_thermostat_temperature.assert_not_called()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_engine_vacation_mode_single_within_range_turns_off():
    """Single mode, temp within bounds: HVAC turned off."""
    ha = _make_ha(hvac_mode="heat", current_temp=72.0)
    engine = _make_engine(ha, vacation_mode=True)

    conn = await _setup_db()
    try:
        await _insert_room(conn, "room1", THERMO_A)
        tc = ThermostatConfig(
            thermostat_entity_id=THERMO_A,
            min_setpoint=62.0,
            max_setpoint=80.0,
            vacation_hvac_mode="single",
        )
        await db.upsert_thermostat_config(conn, tc)

        await engine.tick(conn)

        ha.set_thermostat_hvac_mode.assert_called_once_with(THERMO_A, "off")
        ha.set_thermostat_temperature.assert_not_called()
        ha.set_thermostat_temperature_range.assert_not_called()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_engine_vacation_mode_single_below_min_heats():
    """Single mode, temp below min_setpoint: heat to min_setpoint."""
    ha = _make_ha(hvac_mode="off", current_temp=58.0)
    engine = _make_engine(ha, vacation_mode=True)

    conn = await _setup_db()
    try:
        await _insert_room(conn, "room1", THERMO_A)
        tc = ThermostatConfig(
            thermostat_entity_id=THERMO_A,
            min_setpoint=62.0,
            max_setpoint=80.0,
            vacation_hvac_mode="single",
        )
        await db.upsert_thermostat_config(conn, tc)

        await engine.tick(conn)

        ha.set_thermostat_temperature.assert_called_once_with(THERMO_A, 62.0, hvac_mode="heat")
        ha.set_thermostat_hvac_mode.assert_not_called()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_engine_vacation_mode_single_above_max_cools():
    """Single mode, temp above max_setpoint: cool to max_setpoint."""
    ha = _make_ha(hvac_mode="off", current_temp=85.0)
    engine = _make_engine(ha, vacation_mode=True)

    conn = await _setup_db()
    try:
        await _insert_room(conn, "room1", THERMO_A)
        tc = ThermostatConfig(
            thermostat_entity_id=THERMO_A,
            min_setpoint=62.0,
            max_setpoint=80.0,
            vacation_hvac_mode="single",
        )
        await db.upsert_thermostat_config(conn, tc)

        await engine.tick(conn)

        ha.set_thermostat_temperature.assert_called_once_with(THERMO_A, 80.0, hvac_mode="cool")
        ha.set_thermostat_hvac_mode.assert_not_called()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_engine_vacation_mode_already_off_no_redundant_call():
    """Single mode, temp within range and HVAC already off: no call made."""
    ha = _make_ha(hvac_mode="off", current_temp=72.0)
    engine = _make_engine(ha, vacation_mode=True)

    conn = await _setup_db()
    try:
        await _insert_room(conn, "room1", THERMO_A)
        tc = ThermostatConfig(
            thermostat_entity_id=THERMO_A,
            min_setpoint=62.0,
            max_setpoint=80.0,
            vacation_hvac_mode="single",
        )
        await db.upsert_thermostat_config(conn, tc)

        await engine.tick(conn)

        ha.set_thermostat_hvac_mode.assert_not_called()
        ha.set_thermostat_temperature.assert_not_called()
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Engine reverts heat_cool when vacation mode is NOT active (test-button fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_reverts_heat_cool_when_vacation_inactive():
    """If the thermostat is in heat_cool mode and vacation is OFF, the engine
    should immediately revert it to 'off'. This covers the case where the user
    clicked the 'Test auto mode' button but never enabled vacation mode."""
    ha = _make_ha(hvac_mode="heat_cool", current_temp=72.0)
    ha.get_state.return_value = {
        "state": "heat_cool",
        "attributes": {
            "current_temperature": 72.0,
            "temperature": 72.0,
            "hvac_action": "idle",
        },
    }
    engine = _make_engine(ha, vacation_mode=False)  # vacation NOT active

    conn = await _setup_db()
    try:
        await _insert_room(conn, "room1", THERMO_A)
        tc = ThermostatConfig(
            thermostat_entity_id=THERMO_A,
            min_setpoint=62.0,
            max_setpoint=80.0,
            vacation_hvac_mode="range",
        )
        await db.upsert_thermostat_config(conn, tc)

        await engine.tick(conn)

        ha.set_thermostat_hvac_mode.assert_called_once_with(THERMO_A, "off")
        ha.set_thermostat_temperature_range.assert_not_called()
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Celsius mode: setback temps stored in °F, displayed correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_vacation_mode_celsius_single_below_min():
    """Vacation hold always uses stored °F values regardless of unit."""
    ha = _make_ha(
        hvac_mode="off", current_temp=12.0
    )  # 12°C raw from HA (already converted to °F by ha_client)
    # Simulate: current_temperature is returned by HA in °F after normalisation
    ha.get_state.return_value = {
        "state": "off",
        "attributes": {"current_temperature": 53.6, "temperature": 53.6, "hvac_action": "idle"},
    }
    engine = _make_engine(ha, vacation_mode=True)

    conn = await _setup_db()
    try:
        await _insert_room(conn, "room1", THERMO_A)
        # min_setpoint stored in °F (62°F ≈ 16.7°C)
        tc = ThermostatConfig(
            thermostat_entity_id=THERMO_A,
            min_setpoint=62.0,
            max_setpoint=80.0,
            vacation_hvac_mode="single",
        )
        await db.upsert_thermostat_config(conn, tc)

        await engine.tick(conn)

        # Should heat to 62°F (stored in °F regardless of unit)
        ha.set_thermostat_temperature.assert_called_once_with(THERMO_A, 62.0, hvac_mode="heat")
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# HVAC resumes after vacation ends (regression for post-vacation off-state bug)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_resumes_cooling_after_vacation_single_ends():
    """After vacation single mode leaves thermostat 'off', the engine must start
    a cooling cycle on the first tick once vacation_mode is False and rooms are
    calling for cooling via presence holdover."""
    # Thermostat is "off" — exactly the state vacation single-mode leaves it in
    # when the house temperature was within the safe band at vacation end.
    ha = _make_ha(hvac_mode="off", current_temp=78.0)
    ha.get_state.return_value = {
        "state": "off",
        "attributes": {"current_temperature": 78.0, "temperature": 72.0, "hvac_action": "idle"},
    }
    vent_ctrl = VentController(ha)
    engine = CycleEngine(
        thermostat_entity_id=THERMO_A,
        ha=ha,
        vent_ctrl=vent_ctrl,
        get_enabled=lambda: True,
        get_vacation_mode=lambda: False,
    )

    conn = await _setup_db()
    try:
        # Room with a presence target of 70 °F and holdover enabled.
        room = Room(
            id="room1",
            name="Living Room",
            thermostat_entity_id=THERMO_A,
            system_wide_temp=70.0,
            presence_holdover_hours=2.0,
        )
        await db.upsert_room(conn, room)

        # Plant an active presence holdover (simulates someone returning from vacation).
        now = datetime.now(UTC)
        await db.upsert_holdover_state(
            conn,
            PresenceHoldoverState(
                room_id="room1",
                last_detected_at=now,
                expires_at=now + timedelta(hours=2),
            ),
        )

        tc = ThermostatConfig(
            thermostat_entity_id=THERMO_A,
            min_setpoint=62.0,
            max_setpoint=80.0,
            vacation_hvac_mode="single",
            deadband=0.5,
            overshoot_delta=2.0,
        )
        await db.upsert_thermostat_config(conn, tc)

        await engine.tick(conn)

        # Room needs cooling (78 °F > 70 °F target + 0.5 deadband).
        # First call: cycle-start setpoint 68.0 with hvac_mode="cool" activates the HVAC.
        # (A second call may follow from _terminate_cycle resetting to ambient when no
        # sensor readings are present to confirm room progress — that is expected.)
        first_call = ha.set_thermostat_temperature.call_args_list[0]
        assert first_call == ((THERMO_A, 68.0), {"hvac_mode": "cool"})
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_engine_resumes_cooling_after_vacation_range_reverted():
    """After vacation range mode is reverted (thermostat set to 'off' on the
    prior tick), the engine must start a cooling cycle on the next tick when
    rooms have active presence holdovers calling for cooling."""
    # Thermostat is "off" — the heat_cool revert guard already ran on the
    # immediately-preceding tick (vacation end → heat_cool → off → return).
    ha = _make_ha(hvac_mode="off", current_temp=78.0)
    ha.get_state.return_value = {
        "state": "off",
        "attributes": {"current_temperature": 78.0, "temperature": 72.0, "hvac_action": "idle"},
    }
    vent_ctrl = VentController(ha)
    engine = CycleEngine(
        thermostat_entity_id=THERMO_A,
        ha=ha,
        vent_ctrl=vent_ctrl,
        get_enabled=lambda: True,
        get_vacation_mode=lambda: False,
    )

    conn = await _setup_db()
    try:
        room = Room(
            id="room1",
            name="Living Room",
            thermostat_entity_id=THERMO_A,
            system_wide_temp=70.0,
            presence_holdover_hours=2.0,
        )
        await db.upsert_room(conn, room)

        now = datetime.now(UTC)
        await db.upsert_holdover_state(
            conn,
            PresenceHoldoverState(
                room_id="room1",
                last_detected_at=now,
                expires_at=now + timedelta(hours=2),
            ),
        )

        tc = ThermostatConfig(
            thermostat_entity_id=THERMO_A,
            min_setpoint=62.0,
            max_setpoint=80.0,
            vacation_hvac_mode="range",
            deadband=0.5,
            overshoot_delta=2.0,
        )
        await db.upsert_thermostat_config(conn, tc)

        await engine.tick(conn)

        first_call = ha.set_thermostat_temperature.call_args_list[0]
        assert first_call == ((THERMO_A, 68.0), {"hvac_mode": "cool"})
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Safety-bounds enforcement after vacation ends
# (temp drifts outside min/max setpoint before engine recovers)
# ---------------------------------------------------------------------------


def _make_engine_with_vent_ctrl(ha: MagicMock, vacation_mode: bool = False) -> CycleEngine:
    """Like _make_engine but uses a real VentController so cycle-start succeeds."""
    return CycleEngine(
        thermostat_entity_id=THERMO_A,
        ha=ha,
        vent_ctrl=VentController(ha),
        get_enabled=lambda: True,
        get_vacation_mode=lambda: vacation_mode,
    )


async def _setup_hot_room_with_holdover(conn: aiosqlite.Connection) -> None:
    """Insert a room targeting 70 °F with an active presence holdover."""
    room = Room(
        id="room1",
        name="Living Room",
        thermostat_entity_id=THERMO_A,
        system_wide_temp=70.0,
        presence_holdover_hours=2.0,
    )
    await db.upsert_room(conn, room)
    now = datetime.now(UTC)
    await db.upsert_holdover_state(
        conn,
        PresenceHoldoverState(
            room_id="room1",
            last_detected_at=now,
            expires_at=now + timedelta(hours=2),
        ),
    )


async def _setup_cold_room_with_holdover(conn: aiosqlite.Connection) -> None:
    """Insert a room targeting 68 °F with an active presence holdover."""
    room = Room(
        id="room1",
        name="Living Room",
        thermostat_entity_id=THERMO_A,
        system_wide_temp=68.0,
        presence_holdover_hours=2.0,
    )
    await db.upsert_room(conn, room)
    now = datetime.now(UTC)
    await db.upsert_holdover_state(
        conn,
        PresenceHoldoverState(
            room_id="room1",
            last_detected_at=now,
            expires_at=now + timedelta(hours=2),
        ),
    )


@pytest.mark.asyncio
async def test_engine_cools_above_safety_max_after_single_mode_vacation():
    """After vacation single mode ends (thermostat 'off'), if the house temp
    has risen above max_setpoint the engine must start a cooling cycle to bring
    it back within the configured safety band.

    Single mode leaves the thermostat 'off' when the house was within bounds at
    vacation end.  Temp can rise above max_setpoint between vacation-end and the
    next engine tick (e.g. summer heat build-up while AC was idle).

    Math: current 82 °F > room target 70 °F + deadband 0.5 → "cooling".
    Setpoint = 70 − 2 (overshoot_delta) = 68 °F (within [62, 80] bounds, no clamp).
    """
    ha = _make_ha(hvac_mode="off", current_temp=82.0)
    ha.get_state.return_value = {
        "state": "off",
        "attributes": {"current_temperature": 82.0, "temperature": 72.0, "hvac_action": "idle"},
    }
    engine = _make_engine_with_vent_ctrl(ha, vacation_mode=False)

    conn = await _setup_db()
    try:
        await _setup_hot_room_with_holdover(conn)
        await db.upsert_thermostat_config(
            conn,
            ThermostatConfig(
                thermostat_entity_id=THERMO_A,
                min_setpoint=62.0,
                max_setpoint=80.0,
                vacation_hvac_mode="single",
                deadband=0.5,
                overshoot_delta=2.0,
            ),
        )

        await engine.tick(conn)

        first_call = ha.set_thermostat_temperature.call_args_list[0]
        assert first_call == ((THERMO_A, 68.0), {"hvac_mode": "cool"})
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_engine_cools_above_safety_max_after_range_mode_vacation():
    """After vacation range mode ends and heat_cool has been reverted to 'off'
    on the prior tick, if the house temp exceeds max_setpoint the engine must
    start a cooling cycle.

    Range mode leaves the thermostat in heat_cool; the first post-vacation tick
    reverts it to 'off'.  This test simulates the subsequent tick where the
    thermostat is already 'off' and the house has overheated.

    Math: same as single-mode variant — setpoint = 68 °F, hvac_mode = 'cool'.
    """
    ha = _make_ha(hvac_mode="off", current_temp=82.0)
    ha.get_state.return_value = {
        "state": "off",
        "attributes": {"current_temperature": 82.0, "temperature": 72.0, "hvac_action": "idle"},
    }
    engine = _make_engine_with_vent_ctrl(ha, vacation_mode=False)

    conn = await _setup_db()
    try:
        await _setup_hot_room_with_holdover(conn)
        await db.upsert_thermostat_config(
            conn,
            ThermostatConfig(
                thermostat_entity_id=THERMO_A,
                min_setpoint=62.0,
                max_setpoint=80.0,
                vacation_hvac_mode="range",
                deadband=0.5,
                overshoot_delta=2.0,
            ),
        )

        await engine.tick(conn)

        first_call = ha.set_thermostat_temperature.call_args_list[0]
        assert first_call == ((THERMO_A, 68.0), {"hvac_mode": "cool"})
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_engine_heats_below_safety_min_after_single_mode_vacation():
    """After vacation single mode ends (thermostat 'off'), if the house temp
    has dropped below min_setpoint the engine must start a heating cycle to
    bring it back within the configured safety band.

    Math: current 58 °F < room target 68 °F − deadband 0.5 → "heating".
    Setpoint = 68 + 2 (overshoot_delta) = 70 °F (within [62, 80] bounds, no clamp).
    """
    ha = _make_ha(hvac_mode="off", current_temp=58.0)
    ha.get_state.return_value = {
        "state": "off",
        "attributes": {"current_temperature": 58.0, "temperature": 65.0, "hvac_action": "idle"},
    }
    engine = _make_engine_with_vent_ctrl(ha, vacation_mode=False)

    conn = await _setup_db()
    try:
        await _setup_cold_room_with_holdover(conn)
        await db.upsert_thermostat_config(
            conn,
            ThermostatConfig(
                thermostat_entity_id=THERMO_A,
                min_setpoint=62.0,
                max_setpoint=80.0,
                vacation_hvac_mode="single",
                deadband=0.5,
                overshoot_delta=2.0,
            ),
        )

        await engine.tick(conn)

        first_call = ha.set_thermostat_temperature.call_args_list[0]
        assert first_call == ((THERMO_A, 70.0), {"hvac_mode": "heat"})
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_engine_heats_below_safety_min_after_range_mode_vacation():
    """After vacation range mode ends and heat_cool has been reverted to 'off',
    if the house temp drops below min_setpoint the engine must start a heating
    cycle to bring it back within the configured safety band.

    Math: same as single-mode variant — setpoint = 70 °F, hvac_mode = 'heat'.
    """
    ha = _make_ha(hvac_mode="off", current_temp=58.0)
    ha.get_state.return_value = {
        "state": "off",
        "attributes": {"current_temperature": 58.0, "temperature": 65.0, "hvac_action": "idle"},
    }
    engine = _make_engine_with_vent_ctrl(ha, vacation_mode=False)

    conn = await _setup_db()
    try:
        await _setup_cold_room_with_holdover(conn)
        await db.upsert_thermostat_config(
            conn,
            ThermostatConfig(
                thermostat_entity_id=THERMO_A,
                min_setpoint=62.0,
                max_setpoint=80.0,
                vacation_hvac_mode="range",
                deadband=0.5,
                overshoot_delta=2.0,
            ),
        )

        await engine.tick(conn)

        first_call = ha.set_thermostat_temperature.call_args_list[0]
        assert first_call == ((THERMO_A, 70.0), {"hvac_mode": "heat"})
    finally:
        await conn.close()
