"""
Tests for the one-shot flair.db → app.db rename helper in backend.main,
and for sentinel-guarded data migrations in backend.db.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path

import aiosqlite
import pytest

from backend import db
from backend.engine.cycle_engine import _effective_deadband
from backend.main import _migrate_db_filename
from backend.models import Schedule, ThermostatConfig

_SHORT_CYCLE_SENTINEL = "migration_short_cycle_defaults_v1"

# The exact `rooms` schema as it shipped *before* the per-room deadband override
# (Issue #277) — i.e. the current schema minus the `deadband_override` column.
# Used to simulate an upgrade: an existing DB whose rooms table predates the new
# column, where the column is added only by the ALTER in db.MIGRATIONS.
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
    # new column arrives solely via the ALTER in db.MIGRATIONS.
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


# ---------------------------------------------------------------------------
# Per-schedule deadband override persistence (Issue #517)
#
# `upsert_schedule` is an INSERT … ON CONFLICT DO UPDATE. A nullable column
# that is present in the INSERT list but MISSING from the ON CONFLICT SET list
# writes fine on create and then silently freezes on every later edit — the
# classic half-wired-column bug. These round-trips pin both halves in both
# directions.
# ---------------------------------------------------------------------------


async def _db_with_room(room_id: str = "r1") -> aiosqlite.Connection:
    """A fresh DB holding one room — `schedules.room_id` is a FK to `rooms`."""
    from backend.models import Room

    conn = await _fresh_db()
    await db.upsert_room(
        conn, Room(id=room_id, name="Bedroom", thermostat_entity_id="climate.house")
    )
    return conn


def _block(sid: str = "s1", room_id: str = "r1", **kwargs) -> Schedule:
    s = Schedule.create(
        room_id=room_id,
        days_of_week=[0, 1, 2],
        start_time=time(8, 0),
        end_time=time(17, 0),
        target_temp=70.0,
        **kwargs,
    )
    s.id = sid
    return s


async def _raw_band(conn: aiosqlite.Connection, sid: str) -> object:
    async with conn.execute("SELECT deadband_override FROM schedules WHERE id=?", (sid,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    return row[0]


@pytest.mark.asyncio
async def test_schedule_band_round_trips_through_the_db() -> None:
    conn = await _db_with_room()
    try:
        await db.upsert_schedule(conn, _block(deadband_override=2.5))
        assert (await db.get_schedules_for_room(conn, "r1"))[0].deadband_override == 2.5
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_schedule_band_zero_round_trips_and_is_not_null() -> None:
    """0.0 is a real (exact-match) band, distinct from "no override"."""
    conn = await _db_with_room()
    try:
        await db.upsert_schedule(conn, _block(deadband_override=0.0))
        assert await _raw_band(conn, "s1") == 0.0
        assert (await db.get_schedules_for_room(conn, "r1"))[0].deadband_override == 0.0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_schedule_band_none_persists_as_sql_null() -> None:
    conn = await _db_with_room()
    try:
        await db.upsert_schedule(conn, _block(deadband_override=None))
        assert await _raw_band(conn, "s1") is None
        assert (await db.get_schedules_for_room(conn, "r1"))[0].deadband_override is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_schedule_band_update_value_to_none_sticks() -> None:
    """Clearing a band on an EXISTING row proves the column is in the ON
    CONFLICT SET list — with it missing, the old value would survive."""
    conn = await _db_with_room()
    try:
        await db.upsert_schedule(conn, _block(deadband_override=2.5))
        stored = (await db.get_schedules_for_room(conn, "r1"))[0]
        stored.deadband_override = None
        await db.upsert_schedule(conn, stored)

        assert await _raw_band(conn, "s1") is None
        assert (await db.get_schedules_for_room(conn, "r1"))[0].deadband_override is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_schedule_band_update_none_to_value_sticks() -> None:
    conn = await _db_with_room()
    try:
        await db.upsert_schedule(conn, _block(deadband_override=None))
        stored = (await db.get_schedules_for_room(conn, "r1"))[0]
        stored.deadband_override = 4.0
        await db.upsert_schedule(conn, stored)

        assert (await db.get_schedules_for_room(conn, "r1"))[0].deadband_override == 4.0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_schedule_band_survives_an_unrelated_edit() -> None:
    """Editing another field (target_temp) must not drop the band — the same
    upsert rewrites every column."""
    conn = await _db_with_room()
    try:
        await db.upsert_schedule(conn, _block(deadband_override=1.5))
        stored = (await db.get_schedules_for_room(conn, "r1"))[0]
        stored.target_temp = 72.0
        await db.upsert_schedule(conn, stored)

        reloaded = (await db.get_schedules_for_room(conn, "r1"))[0]
        assert reloaded.target_temp == 72.0
        assert reloaded.deadband_override == 1.5
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_upsert_schedule_statement_carries_the_column_on_both_halves() -> None:
    """Belt-and-braces on the SQL itself: `deadband_override` must appear in
    the INSERT column list AND in the ON CONFLICT update list."""
    import inspect as _inspect

    sql = _inspect.getsource(db.upsert_schedule)
    insert_half, _, conflict_half = sql.partition("ON CONFLICT")
    assert "deadband_override" in insert_half, "missing from the INSERT column list"
    assert "deadband_override=excluded.deadband_override" in conflict_half, (
        "missing from the ON CONFLICT update list — edits would silently no-op"
    )


@pytest.mark.asyncio
async def test_fresh_install_schedule_defaults_to_no_band() -> None:
    """A brand-new install defaults blocks to no band, so fresh and upgraded
    installs behave identically."""
    conn = await _db_with_room()
    try:
        await db.upsert_schedule(conn, _block())
        assert (await db.get_schedules_for_room(conn, "r1"))[0].deadband_override is None
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Coverage additions: statement-effect introspection, backup pruning failure,
# and the holdover local→UTC timestamp shift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_statement_effect_unrecognised_sql_returns_none() -> None:
    """Anything but ALTER TABLE ADD/DROP COLUMN cannot be introspected — the
    caller must treat it as not-yet-applied (None), never as applied."""
    conn = await _fresh_db()
    try:
        result = await db._statement_effect_present(
            conn, "CREATE INDEX IF NOT EXISTS idx_x ON rooms(name)"
        )
        assert result is None
    finally:
        await conn.close()


def test_prune_old_backups_survives_unlink_failure(tmp_path: Path, monkeypatch, caplog) -> None:
    """Backup pruning is best-effort: an OSError while deleting must be logged
    and swallowed, never propagate into startup."""
    import logging
    import os as _os

    db_file = tmp_path / "app.db"
    db_file.write_bytes(b"x")
    for v in (1, 2, 3, 4, 5):
        (tmp_path / f"app.db.pre-migration-v{v}.bak").write_bytes(b"x")

    def _boom(_path):
        raise OSError("permission denied")

    monkeypatch.setattr(_os, "unlink", _boom)
    with caplog.at_level(logging.WARNING, logger="backend.db"):
        db._prune_old_backups(str(db_file), keep=3)  # must not raise
    assert any("Could not prune old pre-migration backups" in r.message for r in caplog.records)
    # Nothing was deleted (the failure hit the first candidate).
    assert len(list(tmp_path.glob("*.bak"))) == 5


@pytest.mark.asyncio
async def test_holdover_timestamps_shifted_local_to_utc(monkeypatch) -> None:
    """With the server in a non-UTC zone, the one-time migration must shift
    stored naive-local holdover stamps by the UTC offset and stamp the
    sentinel so it never reruns."""
    import os as _os
    import time as _time
    from datetime import datetime

    conn = await _fresh_db()
    try:
        # Reset the sentinel that _fresh_db's init_db already stamped.
        await conn.execute(
            "DELETE FROM system_settings WHERE key='migration_holdover_timestamps_utc_v1'"
        )
        await conn.execute(
            "INSERT INTO rooms (id, name, thermostat_entity_id) VALUES ('r1', 'R', 'climate.t')"
        )
        naive_local = "2026-04-13T10:00:00"
        await conn.execute(
            "INSERT INTO presence_holdover_state (room_id, last_detected_at, expires_at)"
            " VALUES ('r1', ?, ?)",
            (naive_local, "2026-04-13T12:00:00"),
        )
        await conn.commit()

        _os.environ["TZ"] = "Etc/GMT+5"  # UTC-5, no DST
        _time.tzset()
        try:
            await db._migrate_holdover_timestamps_to_utc(conn)
        finally:
            _os.environ["TZ"] = "UTC"
            _time.tzset()

        async with conn.execute(
            "SELECT last_detected_at, expires_at FROM presence_holdover_state WHERE room_id='r1'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        # UTC-5 local 10:00 → 15:00 UTC (offset measured between two clock
        # reads, so allow sub-second skew).
        shifted = datetime.fromisoformat(row["last_detected_at"])
        expected = datetime(2026, 4, 13, 15, 0, 0)
        assert abs((shifted - expected).total_seconds()) < 2
        shifted_exp = datetime.fromisoformat(row["expires_at"])
        assert abs((shifted_exp - datetime(2026, 4, 13, 17, 0, 0)).total_seconds()) < 2

        # Sentinel stamped → a rerun must not double-shift.
        await db._migrate_holdover_timestamps_to_utc(conn)
        async with conn.execute(
            "SELECT last_detected_at FROM presence_holdover_state WHERE room_id='r1'"
        ) as cur:
            row2 = await cur.fetchone()
        assert row2 is not None
        assert row2["last_detected_at"] == row["last_detected_at"]
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# metrics_retention_days seeding (Issue #617)
# ---------------------------------------------------------------------------

_METRICS_RETENTION_SENTINEL = "migration_metrics_retention_v1"


async def _rerun_metrics_retention_migration(
    conn: aiosqlite.Connection, cycle_log_retention: str | None
) -> int:
    """Simulate an *existing* install arriving at the #617 upgrade.

    Clears the sentinel and the seeded key so the one-shot runs again over a
    chosen ``cycle_log_retention_days``, then returns the effective metrics
    retention the running system would resolve — read back through the same
    coercion the purge job and the read clamp share, not through a raw SELECT,
    so the assertion is about behaviour rather than about a string in a table.
    """
    await conn.execute(
        "DELETE FROM system_settings WHERE key IN (?,?)",
        (_METRICS_RETENTION_SENTINEL, "metrics_retention_days"),
    )
    if cycle_log_retention is None:
        await conn.execute("DELETE FROM system_settings WHERE key='cycle_log_retention_days'")
    else:
        await db.set_system_setting(conn, "cycle_log_retention_days", cycle_log_retention)
    await conn.commit()
    await db._migrate_metrics_retention(conn)
    return db.coerce_metrics_retention_days(
        await db.get_system_setting(
            conn, "metrics_retention_days", str(db.METRICS_RETENTION_DEFAULT_DAYS)
        )
    )


@pytest.mark.asyncio
async def test_metrics_retention_migration_never_lowers_an_upgrade() -> None:
    """#617 AC7, the load-bearing half.

    An old install at ``cycle_log_retention_days=7`` was deleting cycle logs —
    and therefore every metric — after a week. The upgrade must leave it keeping
    a year, not a week: ``max(existing, 365)`` can only ever preserve MORE data
    than before, which is the only safe direction for a change whose purpose is
    to stop destroying history.
    """
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await db.init_db(conn)
        assert await _rerun_metrics_retention_migration(conn, "7") == 365
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_metrics_retention_migration_keeps_a_longer_existing_window() -> None:
    """An install that had deliberately raised the knob past a year keeps its
    own number — ``max`` never trims it down to the default."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await db.init_db(conn)
        assert await _rerun_metrics_retention_migration(conn, "400") == 400
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_metrics_retention_migration_survives_a_junk_stored_value() -> None:
    """A hand-edited ``cycle_log_retention_days`` must not decide the seed: it
    falls back to the documented default, which the ``max`` then loses to."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await db.init_db(conn)
        assert await _rerun_metrics_retention_migration(conn, "a fortnight") == 365
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_metrics_retention_migration_defaults_when_key_absent() -> None:
    """A fresh DB has no ``cycle_log_retention_days`` row at all; the seed still
    lands on the documented default rather than on nothing."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await db.init_db(conn)
        assert await _rerun_metrics_retention_migration(conn, None) == 365
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_metrics_retention_migration_is_one_shot() -> None:
    """Sentinel-guarded: an operator who *later* lowers the value on purpose —
    the documented way to get the old storage profile back — must not have it
    raised back to 365 on the next restart."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await db.init_db(conn)
        await db.set_system_setting(conn, "metrics_retention_days", "14")
        await db._migrate_metrics_retention(conn)
        assert await db.get_system_setting(conn, "metrics_retention_days") == "14"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_metrics_retention_migration_does_not_overwrite_an_existing_key() -> None:
    """Belt-and-braces: even with the sentinel cleared (a restored backup whose
    settings table survived), an already-configured value wins over the seed."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await db.init_db(conn)
        await conn.execute(
            "DELETE FROM system_settings WHERE key=?", (_METRICS_RETENTION_SENTINEL,)
        )
        await db.set_system_setting(conn, "metrics_retention_days", "21")
        await db._migrate_metrics_retention(conn)
        assert await db.get_system_setting(conn, "metrics_retention_days") == "21"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_init_db_seeds_metrics_retention_on_a_fresh_database() -> None:
    """``init_db`` wires the migration in, so a fresh install comes up with the
    key present rather than relying on every reader's inline default."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await db.init_db(conn)
        assert await db.get_system_setting(conn, "metrics_retention_days") == "365"
    finally:
        await conn.close()


def test_coerce_metrics_retention_days_keeps_zero_and_floors_negatives() -> None:
    """0 means "keep forever" and must survive; unlike its display-window
    sibling, this helper may not clamp it to 1."""
    assert db.coerce_metrics_retention_days("0") == 0
    assert db.coerce_metrics_retention_days("90") == 90
    assert db.coerce_metrics_retention_days("-5") == 0
    assert db.coerce_metrics_retention_days("nonsense") == db.METRICS_RETENTION_DEFAULT_DAYS
    assert db.coerce_metrics_retention_days(None) == db.METRICS_RETENTION_DEFAULT_DAYS


def test_the_log_window_coercions_floor_at_one_day() -> None:
    """The two log windows have no "keep forever" reading — a zero-day Cycle
    History tab would show nothing over data that is still there — so they floor
    at 1 where the metrics window floors at 0."""
    for coerce, default in (
        (db.coerce_event_log_retention_days, db.EVENT_LOG_RETENTION_DEFAULT_DAYS),
        (db.coerce_cycle_log_retention_days, db.CYCLE_LOG_RETENTION_DEFAULT_DAYS),
    ):
        assert coerce("45") == 45
        assert coerce("0") == 1
        assert coerce("-9") == 1
        assert coerce("nonsense") == default
        assert coerce(None) == default


def test_every_retention_coercion_caps_at_the_ceiling() -> None:
    """``date``/``datetime`` arithmetic overflows a little above 739,000 days,
    and every consumer of these settings subtracts them from today. An absurd
    stored value must come back as a number the purge job — awaited unguarded
    during ``Scheduler.start()`` — can actually subtract, or the add-on stops
    booting with no UI left to undo it."""
    from datetime import UTC, datetime, timedelta

    for coerce in (
        db.coerce_metrics_retention_days,
        db.coerce_event_log_retention_days,
        db.coerce_cycle_log_retention_days,
    ):
        assert coerce("999999999") == db.MAX_RETENTION_DAYS
        assert coerce(str(db.MAX_RETENTION_DAYS)) == db.MAX_RETENTION_DAYS
    # The ceiling is a value the arithmetic survives, which is the whole point.
    assert datetime.now(UTC) - timedelta(days=db.MAX_RETENTION_DAYS)


@pytest.mark.asyncio
async def test_metrics_retention_migration_caps_an_absurd_existing_window() -> None:
    """The seed is ``max(existing, 365)``, so an absurd ``cycle_log_retention_days``
    would otherwise be copied straight into the key the purge subtracts from
    today's date."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await db.init_db(conn)
        assert await _rerun_metrics_retention_migration(conn, "999999999") == db.MAX_RETENTION_DAYS
    finally:
        await conn.close()
