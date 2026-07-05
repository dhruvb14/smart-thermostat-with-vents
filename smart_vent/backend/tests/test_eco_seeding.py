"""Unit-aware Eco Mode default seeding (Issue #404).

The migration adds the seven numeric Eco columns to ``thermostat_configs`` with
the round-in-Fahrenheit defaults; ``_migrate_eco_defaults`` then back-fills
existing rows with the values whose *display* reads round in the active unit, so
a °C-mode install sees clean numbers while storage stays °F. Runs once
(sentinel-guarded) and never rewrites values on later boots.
"""

from __future__ import annotations

import aiosqlite
import pytest

from backend import db
from backend.models import ThermostatConfig


async def _fresh_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    return conn


async def _reseed_existing(conn: aiosqlite.Connection, unit: str) -> ThermostatConfig:
    """Simulate an existing install: a thermostat row + last-known unit, with
    the seeding sentinel cleared so the data-migration runs again."""
    await db.upsert_thermostat_config(conn, ThermostatConfig("climate.x"))
    await db.set_system_setting(conn, "temperature_unit", unit)
    await conn.execute("DELETE FROM system_settings WHERE key='migration_eco_defaults_v1'")
    await conn.commit()
    await db._migrate_eco_defaults(conn)
    return await db.get_thermostat_config(conn, "climate.x")


@pytest.mark.asyncio
async def test_fahrenheit_seeding_keeps_round_f_defaults() -> None:
    conn = await _fresh_conn()
    try:
        tc = await _reseed_existing(conn, "F")
        assert tc.eco_cooling_outdoor_threshold == 86.0
        assert tc.eco_cooling_full_drift_temp == 100.0
        assert tc.eco_cooling_max_drift == 4.0
        assert tc.eco_heating_full_drift_temp == 0.0
        assert tc.eco_hysteresis_band == 2.0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_celsius_seeding_stores_round_c_as_fahrenheit() -> None:
    conn = await _fresh_conn()
    try:
        tc = await _reseed_existing(conn, "C")
        # 30/38/Δ2/4/-18/Δ2/Δ1 °C stored as °F.
        assert tc.eco_cooling_outdoor_threshold == 86.0
        assert tc.eco_cooling_full_drift_temp == 100.4
        assert tc.eco_cooling_max_drift == 3.6
        assert tc.eco_heating_outdoor_threshold == 39.2
        assert tc.eco_heating_full_drift_temp == -0.4
        assert tc.eco_hysteresis_band == 1.8
        # …and those °F values read round in °C.
        assert round((tc.eco_cooling_full_drift_temp - 32) * 5 / 9, 1) == 38.0
        assert round(tc.eco_cooling_max_drift * 5 / 9, 1) == 2.0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_seeding_is_idempotent() -> None:
    conn = await _fresh_conn()
    try:
        await _reseed_existing(conn, "C")
        # A second run must not run again (sentinel set) — flip the unit and
        # confirm the stored values are NOT rewritten to the F set.
        await db.set_system_setting(conn, "temperature_unit", "F")
        await db._migrate_eco_defaults(conn)
        tc = await db.get_thermostat_config(conn, "climate.x")
        assert tc.eco_cooling_full_drift_temp == 100.4  # still the °C-derived value
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_fresh_db_seeding_is_a_noop() -> None:
    """No thermostat rows on a fresh DB → the backfill affects nothing."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await db.init_db(conn)  # runs _migrate_eco_defaults with zero rows
        async with conn.execute("SELECT COUNT(*) FROM thermostat_configs") as cur:
            row = await cur.fetchone()
        assert row is not None and row[0] == 0
    finally:
        await conn.close()
