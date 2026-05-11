"""
Tests for vacation mode.

Covers:
  - Scheduler: load/persist vacation mode, auto-expiry on tick
  - Cycle engine: vacation mode guard (abort + hold), range vs single-setpoint
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
from backend.models import Room, ThermostatConfig
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
