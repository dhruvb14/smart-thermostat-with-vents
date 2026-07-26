"""Migration regression tests for the `schedules` table.

Issue #359 — upgrading a pre-#359 database (a `schedules` table without the
`enabled` / `expires_at` columns) backfills existing rows to the pre-#359
behaviour: enabled and never-expiring.

Issue #517 — upgrading a pre-#517 database (the post-#359 table without
`deadband_override`) adds the nullable column and leaves every existing block
inheriting, so cycle behaviour after the upgrade is byte-for-byte what it was
before the feature existed.

Issue #520 — upgrading a pre-#520 database (the post-#517 table without `name`)
adds the nullable column and leaves every existing block unnamed, so it is still
identified by its `id` exactly as it was before the column existed.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from backend import db
from backend.engine.cycle_engine import _effective_deadband
from backend.models import Room

# The `schedules` table exactly as it shipped after #359 and BEFORE the
# per-schedule deadband override — today's schema minus `deadband_override`.
_PRE517_SCHEDULES_SCHEMA = """
CREATE TABLE schedules (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    days_of_week TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    target_temp REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT
)
"""


async def _column_names(conn: aiosqlite.Connection, table: str) -> set[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_pre359_rows_default_enabled_and_never_expire(tmp_path) -> None:
    path = str(tmp_path / "old.db")
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    try:
        # Recreate the pre-#359 schedules table (no enabled / expires_at) and a
        # legacy row, then let init_db run the real ALTER TABLE migrations.
        await conn.execute(
            """
            CREATE TABLE schedules (
                id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                days_of_week TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                target_temp REAL NOT NULL
            )
            """
        )
        await conn.execute(
            "INSERT INTO schedules(id,room_id,days_of_week,start_time,end_time,target_temp) "
            "VALUES (?,?,?,?,?,?)",
            ("s1", "r1", json.dumps([0, 1, 2]), "22:00:00", "07:00:00", 68.0),
        )
        await conn.commit()

        await db.init_db(conn)  # runs the additive migrations

        scheds = await db.get_schedules_for_room(conn, "r1")
        assert len(scheds) == 1
        assert scheds[0].enabled is True  # backfilled to enabled
        assert scheds[0].expires_at is None  # backfilled to never-expire
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Per-schedule deadband override (Issue #517)
# ---------------------------------------------------------------------------


async def _upgraded_db_with_legacy_block(tmp_path) -> aiosqlite.Connection:
    """A pre-#517 `schedules` table carrying one legacy block, upgraded by the
    real ALTER TABLE in db.MIGRATIONS."""
    conn = await aiosqlite.connect(str(tmp_path / "pre517.db"))
    conn.row_factory = aiosqlite.Row
    await conn.execute(_PRE517_SCHEDULES_SCHEMA)
    await conn.execute(
        "INSERT INTO schedules(id,room_id,days_of_week,start_time,end_time,target_temp,"
        "enabled,expires_at) VALUES (?,?,?,?,?,?,?,?)",
        ("s-legacy", "room-legacy", json.dumps([0, 1, 2]), "22:00:00", "07:00:00", 68.0, 1, None),
    )
    await conn.commit()
    await db.init_db(conn)  # runs migration 18
    return conn


@pytest.mark.asyncio
async def test_pre517_upgrade_adds_the_column(tmp_path) -> None:
    conn = await _upgraded_db_with_legacy_block(tmp_path)
    try:
        assert "deadband_override" in await _column_names(conn, "schedules")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_pre517_legacy_block_reads_back_with_no_band(tmp_path) -> None:
    """NULL in the newly added column → None on the model → inherit. The rest
    of the row is untouched by the upgrade."""
    conn = await _upgraded_db_with_legacy_block(tmp_path)
    try:
        scheds = await db.get_schedules_for_room(conn, "room-legacy")
        assert len(scheds) == 1
        assert scheds[0].deadband_override is None
        # Nothing else moved.
        assert scheds[0].target_temp == 68.0
        assert scheds[0].enabled is True
        assert scheds[0].expires_at is None
        assert scheds[0].days_of_week == [0, 1, 2]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_pre517_legacy_block_behaviour_is_unchanged(tmp_path) -> None:
    """The behavioural guarantee: a legacy block resolves to exactly the band
    it resolved to before #517 — the room's override if set, otherwise the
    thermostat's deadband. Passing its (NULL) band changes nothing."""
    conn = await _upgraded_db_with_legacy_block(tmp_path)
    try:
        sched = (await db.get_schedules_for_room(conn, "room-legacy"))[0]
        plain = Room(id="room-legacy", name="Bedroom", thermostat_entity_id="climate.house")
        assert _effective_deadband(plain, 0.5, sched.deadband_override) == 0.5
        assert _effective_deadband(plain, 1.25, sched.deadband_override) == 1.25

        banded_room = Room(
            id="room-legacy",
            name="Bedroom",
            thermostat_entity_id="climate.house",
            deadband_override=2.0,
        )
        assert _effective_deadband(banded_room, 0.5, sched.deadband_override) == 2.0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_pre517_upgrade_is_idempotent(tmp_path) -> None:
    """Re-running init_db on an already-upgraded DB must not re-apply the ALTER
    (SQLite errors on a duplicate column) or disturb the legacy row."""
    conn = await _upgraded_db_with_legacy_block(tmp_path)
    try:
        await db.init_db(conn)
        await db.init_db(conn)

        cols = await _column_names(conn, "schedules")
        assert "deadband_override" in cols
        scheds = await db.get_schedules_for_room(conn, "room-legacy")
        assert len(scheds) == 1
        assert scheds[0].deadband_override is None
        assert scheds[0].target_temp == 68.0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_pre517_upgraded_block_accepts_a_band_afterwards(tmp_path) -> None:
    """An upgraded legacy block is a first-class citizen: setting a band on it
    persists through the new column."""
    conn = await _upgraded_db_with_legacy_block(tmp_path)
    try:
        sched = (await db.get_schedules_for_room(conn, "room-legacy"))[0]
        sched.deadband_override = 3.5
        await db.upsert_schedule(conn, sched)

        reloaded = (await db.get_schedules_for_room(conn, "room-legacy"))[0]
        assert reloaded.deadband_override == 3.5
    finally:
        await conn.close()


# ── Issue #520: optional display name ───────────────────────────────────────
#
# The `schedules` table exactly as it shipped after #517 and BEFORE the display
# name — today's schema minus `name`. Kept separate from _PRE517_SCHEDULES_SCHEMA
# so each upgrade step is exercised from the schema that actually preceded it.
_PRE520_SCHEDULES_SCHEMA = """
CREATE TABLE schedules (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    days_of_week TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    target_temp REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT,
    deadband_override REAL
)
"""


async def _upgraded_db_with_unnamed_block(tmp_path) -> aiosqlite.Connection:
    """A pre-#520 `schedules` table carrying one legacy block, upgraded by the
    real ALTER TABLE in db.MIGRATIONS."""
    conn = await aiosqlite.connect(str(tmp_path / "pre520.db"))
    conn.row_factory = aiosqlite.Row
    await conn.execute(_PRE520_SCHEDULES_SCHEMA)
    await conn.execute(
        "INSERT INTO schedules(id,room_id,days_of_week,start_time,end_time,target_temp,"
        "enabled,expires_at,deadband_override) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "s-legacy",
            "room-legacy",
            json.dumps([0, 1, 2]),
            "22:00:00",
            "07:00:00",
            68.0,
            1,
            None,
            2.0,
        ),
    )
    await conn.commit()
    await db.init_db(conn)  # runs migration 19
    return conn


@pytest.mark.asyncio
async def test_pre520_upgrade_adds_the_column(tmp_path) -> None:
    conn = await _upgraded_db_with_unnamed_block(tmp_path)
    try:
        assert "name" in await _column_names(conn, "schedules")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_pre520_legacy_block_reads_back_unnamed(tmp_path) -> None:
    """NULL in the newly added column → None on the model → the block is still
    identified by its id. The rest of the row is untouched by the upgrade."""
    conn = await _upgraded_db_with_unnamed_block(tmp_path)
    try:
        scheds = await db.get_schedules_for_room(conn, "room-legacy")
        assert len(scheds) == 1
        assert scheds[0].name is None
        assert scheds[0].display_name == "s-legacy"
        # Nothing else moved.
        assert scheds[0].target_temp == 68.0
        assert scheds[0].deadband_override == 2.0
        assert scheds[0].enabled is True
        assert scheds[0].expires_at is None
        assert scheds[0].days_of_week == [0, 1, 2]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_pre520_upgrade_is_idempotent(tmp_path) -> None:
    """Re-running init_db on an already-upgraded DB must not re-apply the ALTER
    (SQLite errors on a duplicate column) or disturb the legacy row."""
    conn = await _upgraded_db_with_unnamed_block(tmp_path)
    try:
        await db.init_db(conn)
        await db.init_db(conn)

        assert "name" in await _column_names(conn, "schedules")
        scheds = await db.get_schedules_for_room(conn, "room-legacy")
        assert len(scheds) == 1
        assert scheds[0].name is None
        assert scheds[0].target_temp == 68.0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_pre520_upgraded_block_accepts_a_name_afterwards(tmp_path) -> None:
    """An upgraded legacy block is a first-class citizen: naming it persists
    through the new column, and its id is unchanged by the rename."""
    conn = await _upgraded_db_with_unnamed_block(tmp_path)
    try:
        sched = (await db.get_schedules_for_room(conn, "room-legacy"))[0]
        sched.name = "Weekday night setback"
        await db.upsert_schedule(conn, sched)

        reloaded = (await db.get_schedules_for_room(conn, "room-legacy"))[0]
        assert reloaded.name == "Weekday night setback"
        assert reloaded.display_name == "Weekday night setback"
        assert reloaded.id == "s-legacy"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_upsert_can_clear_a_name_back_to_none(tmp_path) -> None:
    """The name column is a real round trip in both directions — an upsert that
    clears it must not leave the previous value behind (the ON CONFLICT list
    has to carry the column, which is easy to forget)."""
    conn = await _upgraded_db_with_unnamed_block(tmp_path)
    try:
        sched = (await db.get_schedules_for_room(conn, "room-legacy"))[0]
        sched.name = "Temporary"
        await db.upsert_schedule(conn, sched)
        assert (await db.get_schedules_for_room(conn, "room-legacy"))[0].name == "Temporary"

        sched.name = None
        await db.upsert_schedule(conn, sched)
        assert (await db.get_schedules_for_room(conn, "room-legacy"))[0].name is None
    finally:
        await conn.close()
