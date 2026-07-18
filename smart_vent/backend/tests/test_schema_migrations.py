"""
Tests for the versioned schema-migration system (Issue #21).

Covers: fresh-install stamping, baseline adoption of pre-versioning databases,
idempotency, fail-fast on genuine errors, SCHEMA-snapshot/MIGRATIONS parity,
and the automatic pre-migration file backup + pruning.
"""

from __future__ import annotations

import os
from pathlib import Path

import aiosqlite
import pytest

from backend import db
from backend.db import MIGRATIONS, Migration

# The `rooms` schema as it shipped before the per-room deadband override
# (Issue #277) — the newest historical migration target. A DB carrying only
# this table simulates a real upgrade from an old build: adoption must stamp
# everything already present and apply only the deadband_override migration.
_LEGACY_ROOMS_SCHEMA = """
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

_DEADBAND_OVERRIDE_VERSION = 15  # "Add deadband_override to rooms (Issue #277)"
# The newest migration that adds a column to the (pre-existing) legacy `rooms`
# table, so it is genuinely applied — not adopted as baseline — when a
# pre-versioning DB is upgraded. The pre-migration backup is named for this
# highest pending version. Migration 16 (schedules) does not touch `rooms`, so
# it stays baseline for the legacy fixture; migration 17 (Eco Mode) does.
_ECO_VERSION = 17  # "Eco Mode config, per-room overrides, ... (Issue #404)"
_NEWEST_LEGACY_ROOMS_VERSION = _ECO_VERSION


async def _fetch_migrations(conn: aiosqlite.Connection) -> dict[int, str]:
    async with conn.execute("SELECT version, description FROM schema_migrations") as cur:
        return {row[0]: row[1] for row in await cur.fetchall()}


async def _column_names(conn: aiosqlite.Connection, table: str) -> set[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        return {row[1] for row in await cur.fetchall()}


async def _make_legacy_db(path: str | None = None) -> aiosqlite.Connection:
    """A pre-versioning DB: only the (pre-#277) rooms table plus one row."""
    conn = await aiosqlite.connect(path or ":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_LEGACY_ROOMS_SCHEMA)
    await conn.execute(
        "INSERT INTO rooms (id, name, thermostat_entity_id) VALUES (?, ?, ?)",
        ("room-1", "Bedroom", "climate.house"),
    )
    await conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Static properties of the MIGRATIONS list
# ---------------------------------------------------------------------------


def test_versions_are_unique_and_strictly_increasing() -> None:
    versions = [m.version for m in MIGRATIONS]
    assert versions == sorted(set(versions))
    assert len(versions) == len(set(versions))


def test_every_statement_is_introspectable() -> None:
    """Every historical migration statement must match the ADD/DROP COLUMN
    forms the baseline-adoption introspection understands — otherwise adoption
    of a pre-versioning DB would re-run it. Future non-ALTER migrations are
    allowed, but only from versions appended after the versioning system
    shipped (they will be tracked from day one)."""
    for migration in MIGRATIONS:
        for sql in migration.statements:
            assert db._ADD_COLUMN_RE.match(sql) or db._DROP_COLUMN_RE.match(sql), (
                f"migration {migration.version} statement not introspectable: {sql}"
            )


# ---------------------------------------------------------------------------
# Fresh install and idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_db_stamps_all_migrations_as_baseline() -> None:
    conn = await aiosqlite.connect(":memory:")
    try:
        await db.init_db(conn)
        recorded = await _fetch_migrations(conn)
        assert set(recorded) == {m.version for m in MIGRATIONS}
        # A fresh SCHEMA already contains every migration's effect, so nothing
        # is applied — everything is adopted as baseline.
        assert all(desc.endswith("(baseline)") for desc in recorded.values())
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_init_db_is_idempotent() -> None:
    conn = await aiosqlite.connect(":memory:")
    try:
        await db.init_db(conn)
        await db.init_db(conn)  # second run must not raise or duplicate rows
        recorded = await _fetch_migrations(conn)
        assert len(recorded) == len(MIGRATIONS)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_applied_at_is_recorded() -> None:
    conn = await aiosqlite.connect(":memory:")
    try:
        await db.init_db(conn)
        async with conn.execute("SELECT applied_at FROM schema_migrations") as cur:
            stamps = [row[0] for row in await cur.fetchall()]
        assert stamps and all(stamps)
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# SCHEMA snapshot / MIGRATIONS parity (guards the drift this issue found:
# reconciliation_interval_min and vacation_hvac_mode existed only as ALTERs)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_snapshot_matches_migration_history() -> None:
    """A DB created purely from the SCHEMA snapshot must already contain the
    effect of every migration: added columns present, dropped columns absent."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.executescript(db.SCHEMA)
        for migration in MIGRATIONS:
            for sql in migration.statements:
                if m := db._ADD_COLUMN_RE.match(sql):
                    assert m[2] in await _column_names(conn, m[1]), (
                        f"SCHEMA is missing {m[1]}.{m[2]} (migration {migration.version})"
                    )
                elif m := db._DROP_COLUMN_RE.match(sql):
                    assert m[2] not in await _column_names(conn, m[1]), (
                        f"SCHEMA still contains dropped {m[1]}.{m[2]} "
                        f"(migration {migration.version})"
                    )
                else:
                    # Without this branch a statement neither regex recognizes
                    # (or regex drift) would silently skip verification and the
                    # parity guard would pass vacuously. Force a conscious
                    # update of this test when a new statement shape appears.
                    pytest.fail(
                        f"Migration {migration.version} statement not recognized by "
                        f"the SCHEMA-parity check — extend this test to verify it: {sql!r}"
                    )
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Baseline adoption of a pre-versioning database
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_db_is_adopted_and_pending_migration_applied() -> None:
    conn = await _make_legacy_db()
    try:
        await db.init_db(conn)

        # The one genuinely missing column arrived via the migration…
        assert "deadband_override" in await _column_names(conn, "rooms")
        # …and existing data survived.
        async with conn.execute("SELECT name FROM rooms WHERE id='room-1'") as cur:
            row = await cur.fetchone()
        assert row is not None and row["name"] == "Bedroom"

        # The Eco Mode migration's per-room columns arrived too (they add to the
        # pre-existing rooms table), so it was applied, not adopted as baseline.
        assert "eco_mode_enabled" in await _column_names(conn, "rooms")

        recorded = await _fetch_migrations(conn)
        assert set(recorded) == {m.version for m in MIGRATIONS}
        # Already-present effects were stamped as baseline; the migrations that
        # add columns to the pre-existing rooms table (deadband override, Eco
        # Mode) were actually applied, so they carry the plain description.
        applied = {_DEADBAND_OVERRIDE_VERSION, _ECO_VERSION}
        for version in applied:
            assert not recorded[version].endswith("(baseline)")
        baseline = {v for v, desc in recorded.items() if desc.endswith("(baseline)")}
        assert baseline == {m.version for m in MIGRATIONS} - applied
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_partially_applied_migration_is_completed_not_rerun() -> None:
    """The old runner executed statements independently, so a crash could
    leave a multi-statement migration half-applied. The new runner must skip
    the statements already in effect and apply only the missing ones."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await db.init_db(conn)
        # Simulate: migration 16 (schedules enabled + expires_at) half-applied —
        # expires_at missing, enabled present, and no version row.
        await conn.execute("ALTER TABLE schedules DROP COLUMN expires_at")
        await conn.execute("DELETE FROM schema_migrations WHERE version=16")
        await conn.commit()

        await db.run_migrations(conn)

        assert "expires_at" in await _column_names(conn, "schedules")
        recorded = await _fetch_migrations(conn)
        assert 16 in recorded
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Fail fast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failing_migration_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = Migration(999, "bad migration", ("ALTER TABLE nonexistent ADD COLUMN x TEXT",))
    monkeypatch.setattr(db, "MIGRATIONS", (*MIGRATIONS, bad))
    conn = await aiosqlite.connect(":memory:")
    try:
        with pytest.raises(aiosqlite.OperationalError):
            await db.init_db(conn)
        # The failed migration must not be recorded as applied.
        recorded = await _fetch_migrations(conn)
        assert 999 not in recorded
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Pre-migration file backup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backup_written_before_pending_migrations(tmp_path: Path) -> None:
    db_file = tmp_path / "app.db"
    conn = await _make_legacy_db(str(db_file))
    try:
        await db.init_db(conn)
    finally:
        await conn.close()

    backup = tmp_path / f"app.db.pre-migration-v{_NEWEST_LEGACY_ROOMS_VERSION}.bak"
    assert backup.exists()

    # The backup is the PRE-migration state: the legacy row is there, the
    # migrated column is not — exactly what a manual rollback needs.
    bconn = await aiosqlite.connect(str(backup))
    try:
        async with bconn.execute("SELECT COUNT(*) FROM rooms") as cur:
            row = await cur.fetchone()
        assert row is not None and row[0] == 1
        assert "deadband_override" not in await _column_names(bconn, "rooms")
    finally:
        await bconn.close()


@pytest.mark.asyncio
async def test_no_backup_for_fresh_file_db(tmp_path: Path) -> None:
    conn = await aiosqlite.connect(str(tmp_path / "app.db"))
    try:
        await db.init_db(conn)
    finally:
        await conn.close()
    assert not list(tmp_path.glob("*.bak"))


@pytest.mark.asyncio
async def test_existing_backup_is_not_overwritten(tmp_path: Path) -> None:
    """A crash-loop (backup → migration fails → restart) must not replace the
    good snapshot with the half-migrated database."""
    db_file = tmp_path / "app.db"
    backup = tmp_path / f"app.db.pre-migration-v{_NEWEST_LEGACY_ROOMS_VERSION}.bak"
    backup.write_bytes(b"KEEP-ME")

    conn = await _make_legacy_db(str(db_file))
    try:
        await db.init_db(conn)
        assert "deadband_override" in await _column_names(conn, "rooms")
    finally:
        await conn.close()

    assert backup.read_bytes() == b"KEEP-ME"


@pytest.mark.asyncio
async def test_old_backups_are_pruned(tmp_path: Path) -> None:
    db_file = tmp_path / "app.db"
    # Four stale backups from earlier upgrades, oldest first.
    for i, version in enumerate((3, 5, 7, 9)):
        stale = tmp_path / f"app.db.pre-migration-v{version}.bak"
        stale.write_bytes(b"old")
        os.utime(stale, (1_000_000 + i, 1_000_000 + i))

    conn = await _make_legacy_db(str(db_file))
    try:
        await db.init_db(conn)
    finally:
        await conn.close()

    backups = sorted(p.name for p in tmp_path.glob("*.bak"))
    assert len(backups) == db._BACKUP_KEEP
    # The newest (just-written) backup survives; the oldest ones are gone.
    assert f"app.db.pre-migration-v{_NEWEST_LEGACY_ROOMS_VERSION}.bak" in backups
    assert "app.db.pre-migration-v3.bak" not in backups
    assert "app.db.pre-migration-v5.bak" not in backups
