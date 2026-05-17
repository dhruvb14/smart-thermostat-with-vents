"""
Tests for the one-shot flair.db → app.db rename helper in backend.main,
and for sentinel-guarded data migrations in backend.db.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from backend import db
from backend.main import _migrate_db_filename
from backend.models import ThermostatConfig

_SHORT_CYCLE_SENTINEL = "migration_short_cycle_defaults_v1"


def test_fresh_dir_is_noop(tmp_path: Path) -> None:
    _migrate_db_filename(str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_rename_when_only_old_present(tmp_path: Path) -> None:
    (tmp_path / "flair.db").write_bytes(b"sqlite-data")

    _migrate_db_filename(str(tmp_path))

    assert not (tmp_path / "flair.db").exists()
    assert (tmp_path / "app.db").read_bytes() == b"sqlite-data"


def test_noop_when_both_present(tmp_path: Path) -> None:
    (tmp_path / "flair.db").write_bytes(b"old-data")
    (tmp_path / "app.db").write_bytes(b"new-data")

    _migrate_db_filename(str(tmp_path))

    assert (tmp_path / "flair.db").read_bytes() == b"old-data"
    assert (tmp_path / "app.db").read_bytes() == b"new-data"


def test_rename_sidecars(tmp_path: Path) -> None:
    (tmp_path / "flair.db").write_bytes(b"main")
    (tmp_path / "flair.db-wal").write_bytes(b"wal")
    (tmp_path / "flair.db-shm").write_bytes(b"shm")

    _migrate_db_filename(str(tmp_path))

    assert not (tmp_path / "flair.db").exists()
    assert not (tmp_path / "flair.db-wal").exists()
    assert not (tmp_path / "flair.db-shm").exists()
    assert (tmp_path / "app.db").read_bytes() == b"main"
    assert (tmp_path / "app.db-wal").read_bytes() == b"wal"
    assert (tmp_path / "app.db-shm").read_bytes() == b"shm"


def test_idempotent_second_call(tmp_path: Path) -> None:
    (tmp_path / "flair.db").write_bytes(b"data")

    _migrate_db_filename(str(tmp_path))
    _migrate_db_filename(str(tmp_path))

    assert not (tmp_path / "flair.db").exists()
    assert (tmp_path / "app.db").read_bytes() == b"data"


# ---------------------------------------------------------------------------
# Short-cycle protection default back-fill (Issue #208)
# ---------------------------------------------------------------------------


async def _fresh_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    return conn


async def _clear_short_cycle_sentinel(conn: aiosqlite.Connection) -> None:
    """Drop the migration sentinel so _migrate_short_cycle_defaults runs again,
    simulating the first startup after the feature is deployed."""
    await conn.execute("DELETE FROM system_settings WHERE key=?", (_SHORT_CYCLE_SENTINEL,))
    await conn.commit()


@pytest.mark.asyncio
async def test_existing_thermostat_gets_recommended_short_cycle_values() -> None:
    """A thermostat that existed before the feature is back-filled with the
    recommended minimums."""
    conn = await _fresh_db()
    try:
        # A pre-existing thermostat with the short-cycle guards still disabled.
        await db.upsert_thermostat_config(
            conn, ThermostatConfig(thermostat_entity_id="climate.old")
        )
        # Re-run the migration as if for the first time after deploy.
        await _clear_short_cycle_sentinel(conn)
        await db._migrate_short_cycle_defaults(conn)

        tc = await db.get_thermostat_config(conn, "climate.old")
        assert tc.min_cycle_runtime_min == db.RECOMMENDED_MIN_CYCLE_RUNTIME_MIN
        assert tc.min_cycle_offtime_min == db.RECOMMENDED_MIN_CYCLE_OFFTIME_MIN
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_new_thermostat_keeps_disabled_default() -> None:
    """A thermostat registered after the migration has already run keeps the
    0 (disabled) default — it is not back-filled."""
    conn = await _fresh_db()
    try:
        # init_db already ran the migration and set the sentinel. A thermostat
        # created now represents a brand-new registration.
        await db.upsert_thermostat_config(
            conn, ThermostatConfig(thermostat_entity_id="climate.new")
        )
        await db._migrate_short_cycle_defaults(conn)  # sentinel present → no-op

        tc = await db.get_thermostat_config(conn, "climate.new")
        assert tc.min_cycle_runtime_min == 0
        assert tc.min_cycle_offtime_min == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_migration_preserves_user_tuned_values() -> None:
    """The back-fill must not overwrite a thermostat the user already tuned to
    non-zero short-cycle values."""
    conn = await _fresh_db()
    try:
        await db.upsert_thermostat_config(
            conn,
            ThermostatConfig(
                thermostat_entity_id="climate.tuned",
                min_cycle_runtime_min=20,
                min_cycle_offtime_min=8,
            ),
        )
        await _clear_short_cycle_sentinel(conn)
        await db._migrate_short_cycle_defaults(conn)

        tc = await db.get_thermostat_config(conn, "climate.tuned")
        assert tc.min_cycle_runtime_min == 20
        assert tc.min_cycle_offtime_min == 8
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_migration_runs_only_once() -> None:
    """Once the sentinel is set the migration is a no-op, so a value the user
    later resets to 0 is not force-enabled again on the next startup."""
    conn = await _fresh_db()
    try:
        await db.upsert_thermostat_config(conn, ThermostatConfig(thermostat_entity_id="climate.x"))
        # First run back-fills the existing thermostat.
        await _clear_short_cycle_sentinel(conn)
        await db._migrate_short_cycle_defaults(conn)

        # User deliberately disables it again.
        tc = await db.get_thermostat_config(conn, "climate.x")
        tc.min_cycle_runtime_min = 0
        tc.min_cycle_offtime_min = 0
        await db.upsert_thermostat_config(conn, tc)

        # A later startup must not re-enable it (sentinel already set).
        await db._migrate_short_cycle_defaults(conn)
        tc = await db.get_thermostat_config(conn, "climate.x")
        assert tc.min_cycle_runtime_min == 0
        assert tc.min_cycle_offtime_min == 0
    finally:
        await conn.close()
