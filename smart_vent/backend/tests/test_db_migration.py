"""
Tests for the one-shot flair.db → app.db rename helper in backend.main,
and for sentinel-guarded data migrations in backend.db.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from backend import db
from backend.engine.cycle_engine import _effective_deadband
from backend.main import _migrate_db_filename
from backend.models import ThermostatConfig

_SHORT_CYCLE_SENTINEL = "migration_short_cycle_defaults_v1"

# The exact `rooms` schema as it shipped *before* the per-room deadband override
# (Issue #277) — i.e. the current schema minus the `deadband_override` column.
# Used to simulate an upgrade: an existing DB whose rooms table predates the new
# column, where the column is added only by the ALTER in db._MIGRATIONS.
_PRE_DEADBAND_OVERRIDE_ROOMS_SCHEMA = """
CREATE TABLE rooms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    thermostat_entity_id TEXT NOT NULL,
    include_thermostat_sensor INTEGER NOT NULL DEFAULT 0,
    system_wide_temp REAL,
    presence_holdover_hours REAL NOT NULL DEFAULT 2.0,
    notes TEXT NOT NULL DEFAULT '',
    temp_offset REAL NOT NULL DEFAULT 0.0,
    ambient_suppression_enabled INTEGER NOT NULL DEFAULT 0,
    ambient_suppression_mode TEXT NOT NULL DEFAULT 'any_presence',
    ambient_suppression_min_differential REAL NOT NULL DEFAULT 5.0,
    ambient_suppression_deadband REAL NOT NULL DEFAULT 2.0,
    ambient_suppression_off_schedule_window_min INTEGER NOT NULL DEFAULT 60
);
"""


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


# ---------------------------------------------------------------------------
# Per-room deadband override upgrade path (Issue #277)
#
# The override column is nullable; an existing room with no override must keep
# behaving exactly as before — the engine falls back to the thermostat deadband.
# These tests simulate the real upgrade: a rooms table that predates the column,
# carrying a room row, then init_db adds the column via ALTER.
# ---------------------------------------------------------------------------


async def _upgraded_db_with_legacy_room(room_id: str = "room-legacy") -> aiosqlite.Connection:
    """Return a connection whose rooms table started out *without*
    ``deadband_override`` and carried a room row, then was upgraded by
    ``init_db`` (which runs the ALTER migration)."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    # Create the pre-#277 rooms table and insert a room the way an older build
    # would have — no knowledge of deadband_override.
    await conn.executescript(_PRE_DEADBAND_OVERRIDE_ROOMS_SCHEMA)
    await conn.execute(
        "INSERT INTO rooms (id, name, thermostat_entity_id) VALUES (?, ?, ?)",
        (room_id, "Bedroom", "climate.house"),
    )
    await conn.commit()
    # Upgrade. CREATE TABLE IF NOT EXISTS leaves the existing table alone, so the
    # new column arrives solely via the ALTER in db._MIGRATIONS.
    await db.init_db(conn)
    return conn


@pytest.mark.asyncio
async def test_legacy_room_gets_null_override_after_upgrade() -> None:
    """A room that existed before the feature loads with deadband_override=None
    (inherit) after the column is added by the upgrade migration."""
    conn = await _upgraded_db_with_legacy_room()
    try:
        # The column now exists on the upgraded table.
        async with conn.execute("PRAGMA table_info(rooms)") as cur:
            columns = {row["name"] for row in await cur.fetchall()}
        assert "deadband_override" in columns

        room = await db.get_room(conn, "room-legacy")
        assert room is not None
        # NULL in the back-filled column → None in the model → inherit.
        assert room.deadband_override is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_legacy_room_inherits_thermostat_deadband_after_upgrade() -> None:
    """The behavioural guarantee: with no override configured, the engine's
    effective deadband for the room is exactly the thermostat's deadband — i.e.
    cycle decisions are unchanged from before the upgrade."""
    conn = await _upgraded_db_with_legacy_room()
    try:
        room = await db.get_room(conn, "room-legacy")
        assert room is not None
        thermostat_deadband = 0.5  # the long-standing default
        assert _effective_deadband(room, thermostat_deadband) == thermostat_deadband
        # And any thermostat deadband flows through untouched.
        assert _effective_deadband(room, 1.25) == 1.25
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_legacy_room_resaves_with_null_override_preserved() -> None:
    """Editing other fields on a legacy room (a plain upsert of the loaded
    object) must not invent an override — it round-trips as NULL/None."""
    conn = await _upgraded_db_with_legacy_room()
    try:
        room = await db.get_room(conn, "room-legacy")
        assert room is not None
        room.notes = "edited after upgrade"
        await db.upsert_room(conn, room)

        reloaded = await db.get_room(conn, "room-legacy")
        assert reloaded is not None
        assert reloaded.notes == "edited after upgrade"
        assert reloaded.deadband_override is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_fresh_install_room_defaults_to_no_override() -> None:
    """A brand-new install (init_db on an empty DB) also defaults rooms to no
    override, so fresh and upgraded installs behave identically."""
    conn = await _fresh_db()
    try:
        from backend.models import Room

        await db.upsert_room(conn, Room.create(name="New", thermostat_entity_id="climate.house"))
        rooms = await db.get_all_rooms(conn)
        assert len(rooms) == 1
        assert rooms[0].deadband_override is None
        assert _effective_deadband(rooms[0], 0.5) == 0.5
    finally:
        await conn.close()
