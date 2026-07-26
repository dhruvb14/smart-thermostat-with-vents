"""
SQLite database layer using aiosqlite.
All public functions are async and accept an aiosqlite.Connection.
Call `init_db(conn)` once at startup to create tables.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from itertools import groupby

import aiosqlite

from . import eco, tz
from .models import (
    CycleLog,
    CycleSetpointHistory,
    CycleTempSample,
    CycleVentEvent,
    PresenceHoldoverState,
    Room,
    RoomCycleState,
    RoomOverride,
    RoomPresenceSensor,
    RoomSensor,
    RoomVent,
    Schedule,
    ThermostatConfig,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    thermostat_entity_id TEXT NOT NULL,
    include_thermostat_sensor INTEGER NOT NULL DEFAULT 0,
    system_wide_temp REAL,
    presence_holdover_hours REAL NOT NULL DEFAULT 2.0,
    notes TEXT NOT NULL DEFAULT '',
    temp_offset REAL NOT NULL DEFAULT 0.0,
    deadband_override REAL,
    ambient_suppression_enabled INTEGER NOT NULL DEFAULT 0,
    ambient_suppression_mode TEXT NOT NULL DEFAULT 'any_presence',
    ambient_suppression_min_differential REAL NOT NULL DEFAULT 5.0,
    ambient_suppression_deadband REAL NOT NULL DEFAULT 2.0,
    ambient_suppression_off_schedule_window_min INTEGER NOT NULL DEFAULT 60,
    -- Eco Mode per-room overrides (Issue #404). All nullable: NULL = inherit
    -- the thermostat value for that field (field-level null-inheritance).
    eco_mode_enabled INTEGER,
    eco_cooling_outdoor_threshold REAL,
    eco_cooling_full_drift_temp REAL,
    eco_cooling_max_drift REAL,
    eco_heating_outdoor_threshold REAL,
    eco_heating_full_drift_temp REAL,
    eco_heating_max_drift REAL,
    eco_hysteresis_band REAL
);

CREATE TABLE IF NOT EXISTS room_sensors (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL,
    UNIQUE(room_id, entity_id)
);

CREATE TABLE IF NOT EXISTS room_vents (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL,
    control_method TEXT NOT NULL DEFAULT 'open_close',
    UNIQUE(room_id, entity_id)
);

CREATE TABLE IF NOT EXISTS room_presence_sensors (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL,
    UNIQUE(room_id, entity_id)
);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    days_of_week TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    target_temp REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT,  -- naive LOCAL wall-clock ISO; NULL = never expire
    -- Per-schedule deadband override (Issue #517). NULL = inherit the room's
    -- deadband_override, then the thermostat's deadband.
    deadband_override REAL,
    -- Optional display name (Issue #520). NULL = unnamed; callers fall back to
    -- `id`. A label only — not an identifier, and not unique.
    name TEXT
);

CREATE TABLE IF NOT EXISTS thermostat_configs (
    thermostat_entity_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    default_temp REAL,
    min_setpoint REAL NOT NULL DEFAULT 60.0,
    max_setpoint REAL NOT NULL DEFAULT 85.0,
    deadband REAL NOT NULL DEFAULT 0.5,
    max_vent_closed_min INTEGER NOT NULL DEFAULT 0,
    overshoot_delta REAL NOT NULL DEFAULT 2.0,
    cycle_timeout_hours REAL NOT NULL DEFAULT 3.0,
    reconciliation_interval_min INTEGER NOT NULL DEFAULT 0,
    vacation_hvac_mode TEXT NOT NULL DEFAULT 'single',
    min_cycle_runtime_min INTEGER NOT NULL DEFAULT 0,
    min_cycle_offtime_min INTEGER NOT NULL DEFAULT 0,
    cooling_lockout_below_f REAL,
    -- Airflow-floor / dead-head protection (Issue #213). Replaces the prior
    -- count-based ``min_open_vents``. NULL ``total_vents_count`` means the
    -- thermostat predates this change; the engine treats it as the legacy
    -- "≥1 open" default and the Thermostats-page banner nudges the user.
    total_vents_count INTEGER,
    has_bypass_damper INTEGER NOT NULL DEFAULT 0,
    min_open_vents_fraction REAL NOT NULL DEFAULT 0.333,
    overflow_during_min_runtime INTEGER NOT NULL DEFAULT 1,
    unavailable_abort_after_min INTEGER NOT NULL DEFAULT 5,
    -- Eco Mode global per-thermostat config (Issue #404). Defaults are the
    -- round-in-Fahrenheit set; a °C-mode install rewrites them to the
    -- round-in-Celsius equivalents once via _migrate_eco_defaults.
    eco_mode_enabled INTEGER NOT NULL DEFAULT 0,
    eco_cooling_outdoor_threshold REAL NOT NULL DEFAULT 86.0,
    eco_cooling_full_drift_temp REAL NOT NULL DEFAULT 100.0,
    eco_cooling_max_drift REAL NOT NULL DEFAULT 4.0,
    eco_heating_outdoor_threshold REAL NOT NULL DEFAULT 40.0,
    eco_heating_full_drift_temp REAL NOT NULL DEFAULT 0.0,
    eco_heating_max_drift REAL NOT NULL DEFAULT 4.0,
    eco_hysteresis_band REAL NOT NULL DEFAULT 2.0
);

-- Eco Suspend (Issue #500): temporary per-thermostat suspension of Eco Mode.
-- A row means "Eco is suspended for this thermostat until resume_at"; the
-- scheduler sweeps expired rows every tick. Deliberately its OWN table rather
-- than a thermostat_configs column: this is state with an expiry, not tuning
-- config, so the config PUT/upsert path can never clobber it with a stale
-- form snapshot. No FK — a config row may not exist yet for the entity id;
-- delete_thermostat_config removes the suspension alongside the config.
CREATE TABLE IF NOT EXISTS eco_suspensions (
    thermostat_entity_id TEXT PRIMARY KEY,
    resume_at TEXT NOT NULL  -- naive UTC ISO (see _dts)
);

CREATE TABLE IF NOT EXISTS room_overrides (
    room_id TEXT PRIMARY KEY REFERENCES rooms(id) ON DELETE CASCADE,
    target_temp REAL NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS presence_holdover_state (
    room_id TEXT PRIMARY KEY REFERENCES rooms(id) ON DELETE CASCADE,
    last_detected_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- "Clear presence" suppression (#439): while a row exists, presence demand
-- for the room is ignored (no holdover is written by the continuous refresh
-- or by sensor on-edges). The scheduler deletes the row once every presence
-- sensor for the room reads off — the room emptied — re-arming normal
-- presence behavior for the next genuine occupancy.
CREATE TABLE IF NOT EXISTS presence_suppression (
    room_id TEXT PRIMARY KEY REFERENCES rooms(id) ON DELETE CASCADE,
    cleared_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycle_logs (
    id TEXT PRIMARY KEY,
    thermostat_entity_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    mode TEXT NOT NULL,
    rooms_json TEXT NOT NULL DEFAULT '{}',
    ended_reason TEXT,
    thermostat_temp_at_start REAL,
    thermostat_temp_at_end REAL,
    setpoint_at_start REAL,
    setpoint_at_end REAL,
    vents_at_start TEXT,
    vents_at_end TEXT,
    outside_temp_at_start REAL,
    outside_temp_at_end REAL,
    in_min_runtime_hold INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS room_cycle_states (
    cycle_id TEXT NOT NULL REFERENCES cycle_logs(id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    target_temp REAL NOT NULL,
    reached_at TEXT,
    vent_closed_at TEXT,
    temp_at_start REAL,
    temp_at_end REAL,
    trigger_detail TEXT,
    joined_at TEXT,
    role TEXT NOT NULL DEFAULT 'active',
    -- Eco Mode measurability (Issue #404). requested_target = pre-relaxation
    -- target; effective_target = what Eco relaxed it to (equals target_temp);
    -- eco_active = 1 only when Eco actually moved the target this cycle.
    requested_target REAL,
    effective_target REAL,
    eco_active INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (cycle_id, room_id)
);

CREATE TABLE IF NOT EXISTS cycle_temp_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL REFERENCES cycle_logs(id) ON DELETE CASCADE,
    room_id TEXT,
    timestamp TEXT NOT NULL,
    room_temp REAL,
    thermostat_temp REAL,
    setpoint REAL
);

CREATE TABLE IF NOT EXISTS cycle_setpoint_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL REFERENCES cycle_logs(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    setpoint REAL NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS cycle_vent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL REFERENCES cycle_logs(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    room_id TEXT,
    action TEXT NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Per-thermostat per-day rollup (Issue #85 Phase 1a)
CREATE TABLE IF NOT EXISTS daily_thermostat_metrics (
    date TEXT NOT NULL,
    thermostat_entity_id TEXT NOT NULL,
    heating_seconds INTEGER NOT NULL DEFAULT 0,
    cooling_seconds INTEGER NOT NULL DEFAULT 0,
    cycle_count INTEGER NOT NULL DEFAULT 0,
    completed_count INTEGER NOT NULL DEFAULT 0,
    timeout_count INTEGER NOT NULL DEFAULT 0,
    aborted_count INTEGER NOT NULL DEFAULT 0,
    avg_cycle_duration_seconds REAL,
    avg_outside_temp_at_start REAL,
    avg_outside_temp_at_end REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (date, thermostat_entity_id)
);

-- Per-thermostat per-month rollup (Issue #85 Phase 1a)
CREATE TABLE IF NOT EXISTS monthly_thermostat_metrics (
    month TEXT NOT NULL,
    thermostat_entity_id TEXT NOT NULL,
    heating_seconds INTEGER NOT NULL DEFAULT 0,
    cooling_seconds INTEGER NOT NULL DEFAULT 0,
    cycle_count INTEGER NOT NULL DEFAULT 0,
    completed_count INTEGER NOT NULL DEFAULT 0,
    timeout_count INTEGER NOT NULL DEFAULT 0,
    aborted_count INTEGER NOT NULL DEFAULT 0,
    avg_cycle_duration_seconds REAL,
    avg_outside_temp_at_start REAL,
    avg_outside_temp_at_end REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (month, thermostat_entity_id)
);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    level TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT
);

-- MCP bearer tokens (Issue #373). Only the SHA-256 hash of the opaque token is
-- stored; the raw secret is shown once at mint time and never persisted, so a
-- leaked app.db backup cannot be replayed. `scope` is read | write | destructive.
CREATE TABLE IF NOT EXISTS mcp_tokens (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_schedules_room ON schedules(room_id);
CREATE INDEX IF NOT EXISTS idx_cycle_logs_thermostat ON cycle_logs(thermostat_entity_id);
CREATE INDEX IF NOT EXISTS idx_cycle_logs_ended ON cycle_logs(ended_at);
CREATE INDEX IF NOT EXISTS idx_event_log_category ON event_log(category);
CREATE INDEX IF NOT EXISTS idx_event_log_ts ON event_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_cycle_temp_samples_cycle ON cycle_temp_samples(cycle_id, room_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_cycle_setpoint_history_cycle ON cycle_setpoint_history(cycle_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_cycle_vent_events_cycle ON cycle_vent_events(cycle_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_daily_metrics_thermostat ON daily_thermostat_metrics(thermostat_entity_id, date);
CREATE INDEX IF NOT EXISTS idx_monthly_metrics_thermostat ON monthly_thermostat_metrics(thermostat_entity_id, month);
CREATE INDEX IF NOT EXISTS idx_mcp_tokens_hash ON mcp_tokens(token_hash);
"""


async def init_db(conn: aiosqlite.Connection) -> list[tuple[str, str]]:
    """Bring the schema up to date and run the data migrations.

    Returns any room renames the sanitized-name-uniqueness migration had to make
    (Issue #519) so the caller — which, unlike this module, has an event logger —
    can record them where a user will actually see them.
    """
    await run_migrations(conn)
    # Data migration: fix holdover timestamps stored in local time (Issue #65)
    await _migrate_holdover_timestamps_to_utc(conn)
    # Data migration: enable short-cycle protection on pre-existing thermostats
    await _migrate_short_cycle_defaults(conn)
    # Data migration: seed Eco Mode defaults in the active unit (Issue #404)
    await _migrate_eco_defaults(conn)
    # Data migration: force sanitized-name uniqueness across rooms (Issue #519)
    renames = await _migrate_room_name_uniqueness(conn)
    log.info("Database initialised")
    return renames


async def _migrate_room_name_uniqueness(conn: aiosqlite.Connection) -> list[tuple[str, str]]:
    """Force room names to be unique under MQTT sanitisation (Issue #519).

    Room names had no uniqueness constraint (two rooms could both be "Office"),
    but MQTT addresses rooms by sanitised name as well as by id, and sanitising
    is lossy — ``"Office"`` and ``"office"`` collide. The write boundary now
    rejects a colliding create or rename; this repairs installs that already
    have collisions, since refusing to start or leaving the tree ambiguous are
    both worse than an automatic, logged rename.

    The lowest-``rowid`` room in each colliding group keeps its name; the rest
    get ``" (2)"``, ``" (3)"``… appended. A name that sanitises away to nothing
    (punctuation only) is repaired too — it cannot be a topic segment at all.

    Deterministic and idempotent: a second run finds no collisions and changes
    nothing, so it is safe to run on every boot. That also self-heals a DB
    restored from a backup taken before the invariant existed. Returns the
    ``(old, new)`` pairs so the caller can record them in the event log.
    """
    from .mqtt.naming import dedupe_name, sanitize

    async with conn.execute("SELECT rowid, id, name FROM rooms ORDER BY rowid") as cur:
        rows = await cur.fetchall()

    taken: set[str] = set()
    renames: list[tuple[str, str]] = []
    for row in rows:
        name = str(row["name"])
        key = sanitize(name)
        if key and key not in taken:
            taken.add(key)
            continue
        # Collision, or a name with no usable sanitised form at all.
        base = name if key else f"Room {str(row['id'])[:8]}"
        new_name = dedupe_name(base, taken)
        taken.add(sanitize(new_name))
        await conn.execute("UPDATE rooms SET name=? WHERE id=?", (new_name, row["id"]))
        renames.append((name, new_name))

    if renames:
        await conn.commit()
        for old, new in renames:
            log.warning(
                "Room name collision (Issue #519): renamed %r to %r so room names are "
                "unique once sanitised for MQTT topics",
                old,
                new,
            )
    return renames


# ---------------------------------------------------------------------------
# Versioned schema migrations (Issue #21)
#
# ``SCHEMA`` above is the complete fresh-install snapshot; ``MIGRATIONS`` is
# the versioned upgrade path for databases created by older builds. Adding a
# column therefore means touching BOTH: add it to the table in ``SCHEMA`` and
# append a new ``Migration`` here (never edit or delete existing entries —
# ``test_schema_migrations.py`` enforces snapshot/migration parity).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    statements: tuple[str, ...]


_ADD_COLUMN_RE = re.compile(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", re.IGNORECASE)
_DROP_COLUMN_RE = re.compile(r"ALTER TABLE (\w+) DROP COLUMN (\w+)", re.IGNORECASE)

# How many pre-migration backup files to keep next to the DB.
_BACKUP_KEEP = 3


async def _column_exists(conn: aiosqlite.Connection, table: str, column: str) -> bool:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        return any(row[1] == column for row in await cur.fetchall())


async def _statement_effect_present(conn: aiosqlite.Connection, sql: str) -> bool | None:
    """Whether a migration statement's effect is already in the live schema.

    Returns True/False for the recognised ``ALTER TABLE … ADD/DROP COLUMN``
    forms, and None for anything else (cannot be determined by introspection,
    so callers must treat it as not-yet-applied).
    """
    if m := _ADD_COLUMN_RE.match(sql):
        return await _column_exists(conn, m[1], m[2])
    if m := _DROP_COLUMN_RE.match(sql):
        return not await _column_exists(conn, m[1], m[2])
    return None


async def _db_has_user_tables(conn: aiosqlite.Connection) -> bool:
    async with conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ) as cur:
        row = await cur.fetchone()
    return bool(row and row[0] > 0)


async def _stamp_baseline(conn: aiosqlite.Connection) -> set[int]:
    """Adopt a database that predates version tracking.

    The old ad-hoc runner (and ``SCHEMA`` for fresh installs) left no record of
    what ran, so on the first startup with an empty ``schema_migrations`` table
    every migration whose effect is already present in the live schema is
    stamped as applied without re-running it. Returns the stamped versions.
    """
    stamped: set[int] = set()
    for migration in MIGRATIONS:
        checks = [await _statement_effect_present(conn, sql) for sql in migration.statements]
        if all(checks):  # every statement recognised AND already in effect
            await conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (migration.version, f"{migration.description} (baseline)"),
            )
            stamped.add(migration.version)
    await conn.commit()
    if stamped:
        log.info("Adopted existing schema: stamped %d migration(s) as baseline", len(stamped))
    return stamped


def _prune_old_backups(db_file: str, keep: int) -> None:
    """Delete all but the ``keep`` newest pre-migration backups. Best-effort:
    a pruning failure must never block startup."""
    try:
        backups = sorted(
            glob.glob(glob.escape(db_file) + ".pre-migration-v*.bak"),
            key=os.path.getmtime,
            reverse=True,
        )
        for stale in backups[keep:]:
            os.unlink(stale)
            log.info("Pruned old pre-migration backup %s", stale)
    except OSError:
        log.warning("Could not prune old pre-migration backups", exc_info=True)


async def _backup_before_migrations(conn: aiosqlite.Connection, target_version: int) -> str | None:
    """Snapshot the DB file before pending migrations run, so a failed
    migration can be rolled back manually: revert the add-on version, remove
    ``app.db`` (and ``-wal``/``-shm`` sidecars), rename the backup over it.

    Returns the backup path, or None for in-memory/temporary databases.
    """
    async with conn.execute("PRAGMA database_list") as cur:
        rows = await cur.fetchall()
    db_file = next((row[2] for row in rows if row[1] == "main"), "")
    if not db_file:
        return None  # in-memory DB (tests) — nothing to back up

    backup_path = f"{db_file}.pre-migration-v{target_version}.bak"
    if os.path.exists(backup_path):
        # A crash-loop must not overwrite the good snapshot with a
        # half-migrated database — first backup for a target version wins.
        log.info("Pre-migration backup already exists, keeping it: %s", backup_path)
        return backup_path

    await conn.commit()  # VACUUM INTO cannot run inside a transaction
    await conn.execute("VACUUM INTO ?", (backup_path,))
    log.info("Pre-migration DB backup written to %s", backup_path)
    _prune_old_backups(db_file, keep=_BACKUP_KEEP)
    return backup_path


async def run_migrations(conn: aiosqlite.Connection) -> None:
    """Bring the database schema up to date, tracking every step.

    Fresh databases get the full ``SCHEMA`` snapshot and have all migrations
    stamped as baseline; databases from older builds are adopted the same way
    (see ``_stamp_baseline``) and then any genuinely pending migrations are
    applied — after a file backup — exactly once, in version order, failing
    fast instead of silently swallowing errors.
    """
    had_data = await _db_has_user_tables(conn)
    await conn.executescript(SCHEMA)
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               applied_at TEXT NOT NULL DEFAULT (datetime('now')),
               description TEXT NOT NULL
           )"""
    )
    await conn.commit()

    async with conn.execute("SELECT version FROM schema_migrations") as cur:
        applied = {row[0] for row in await cur.fetchall()}
    if not applied:
        applied = await _stamp_baseline(conn)

    pending = [m for m in MIGRATIONS if m.version not in applied]
    if not pending:
        return

    backup_path = None
    if had_data:
        backup_path = await _backup_before_migrations(conn, pending[-1].version)

    current: Migration | None = None
    try:
        for migration in pending:
            current = migration
            for sql in migration.statements:
                if await _statement_effect_present(conn, sql):
                    # Partial application by the old per-statement runner —
                    # recognised via introspection, recorded, not re-run.
                    log.info(
                        "Migration %d: statement already in effect, skipping: %s",
                        migration.version,
                        sql,
                    )
                    continue
                await conn.execute(sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (migration.version, migration.description),
            )
            await conn.commit()
            log.info("Applied migration %d: %s", migration.version, migration.description)
    except Exception:
        rollback_hint = (
            f"a pre-migration backup was saved at {backup_path}. To roll back manually: "
            "stop the add-on, revert to the previous add-on version, delete app.db "
            "(and app.db-wal / app.db-shm) in the data directory, rename the backup "
            "file to app.db, then start the add-on"
            if backup_path
            else "no file backup was taken (empty or in-memory database)"
        )
        log.exception(
            "Schema migration %s failed — startup aborted; %s",
            f"{current.version} ({current.description})" if current else "run",
            rollback_hint,
        )
        raise


async def _migrate_holdover_timestamps_to_utc(conn: aiosqlite.Connection) -> None:
    """One-time migration: shift presence_holdover_state timestamps from server local time to UTC."""
    sentinel = "migration_holdover_timestamps_utc_v1"
    async with conn.execute("SELECT value FROM system_settings WHERE key=?", (sentinel,)) as cur:
        if await cur.fetchone():
            return

    # Compute UTC offset: datetime.now() is intentionally local time here —
    # we need the wall-clock difference between UTC and the server's local timezone
    # to shift old holdover records (stored as naive local timestamps) to UTC.
    offset = datetime.now(UTC).replace(tzinfo=None) - datetime.now()  # noqa: DTZ005

    if abs(offset.total_seconds()) >= 1:
        async with conn.execute(
            "SELECT room_id, last_detected_at, expires_at FROM presence_holdover_state"
        ) as cur:
            rows: list[aiosqlite.Row] = list(await cur.fetchall())

        for row in rows:
            last_detected_at = datetime.fromisoformat(row["last_detected_at"]) + offset
            expires_at = datetime.fromisoformat(row["expires_at"]) + offset
            await conn.execute(
                "UPDATE presence_holdover_state SET last_detected_at=?, expires_at=? WHERE room_id=?",
                (last_detected_at.isoformat(), expires_at.isoformat(), row["room_id"]),
            )

        if rows:
            log.info(
                "Holdover timestamp migration: adjusted %d record(s) from local time to UTC",
                len(rows),
            )
        await conn.commit()

    await conn.execute(
        """INSERT INTO system_settings(key,value) VALUES(?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (sentinel, "1"),
    )
    await conn.commit()


# Short-cycle protection (Issue #208): recommended HVAC minimums applied to
# thermostats that already existed before the feature shipped. New thermostats
# keep the disabled (0) default and the user opts in via the UI, which shows
# these same recommended values.
RECOMMENDED_MIN_CYCLE_RUNTIME_MIN = 10
RECOMMENDED_MIN_CYCLE_OFFTIME_MIN = 5


async def _migrate_short_cycle_defaults(conn: aiosqlite.Connection) -> None:
    """One-time migration: enable short-cycle protection on existing thermostats.

    ``min_cycle_runtime_min`` / ``min_cycle_offtime_min`` are added with a
    column default of 0 (disabled). A thermostat that already had a config row
    before this feature shipped is presumably controlling live equipment, so
    back-fill it with the recommended minimums rather than leaving the
    equipment unprotected until the user happens to find the setting.

    Runs exactly once (sentinel-guarded). Thermostats registered afterwards are
    not touched — they keep the 0 default and the user configures them
    explicitly. Rows the user has already tuned to non-zero values are left
    alone.
    """
    sentinel = "migration_short_cycle_defaults_v1"
    async with conn.execute("SELECT value FROM system_settings WHERE key=?", (sentinel,)) as cur:
        if await cur.fetchone():
            return

    cursor = await conn.execute(
        """UPDATE thermostat_configs
              SET min_cycle_runtime_min=?, min_cycle_offtime_min=?
            WHERE min_cycle_runtime_min=0 AND min_cycle_offtime_min=0""",
        (RECOMMENDED_MIN_CYCLE_RUNTIME_MIN, RECOMMENDED_MIN_CYCLE_OFFTIME_MIN),
    )
    if cursor.rowcount and cursor.rowcount > 0:
        log.info(
            "Short-cycle defaults migration: enabled protection on %d existing "
            "thermostat(s) (runtime=%d min, off-time=%d min)",
            cursor.rowcount,
            RECOMMENDED_MIN_CYCLE_RUNTIME_MIN,
            RECOMMENDED_MIN_CYCLE_OFFTIME_MIN,
        )

    await conn.execute(
        """INSERT INTO system_settings(key,value) VALUES(?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (sentinel, "1"),
    )
    await conn.commit()


async def _migrate_eco_defaults(conn: aiosqlite.Connection) -> None:
    """One-time migration: seed Eco Mode numeric defaults in the active unit.

    The migration adds the seven numeric Eco columns to ``thermostat_configs``
    with the round-in-Fahrenheit defaults. Because a single stored °F value
    can't read round in both units, this back-fills existing rows with the
    values whose *display* reads round in whichever unit is active — so a
    °C-mode user sees clean numbers (30 / 38 / Δ2 …) instead of 37.8 / 2.2.

    Storage stays °F either way; only the seeded numbers change. Eco stays OFF
    (``eco_mode_enabled`` is untouched), so this is behaviourally inert — it
    only affects the default values a user sees when they first open the Eco
    section. Runs exactly once (sentinel-guarded). On a fresh DB there are no
    thermostat rows yet, so it is a no-op and later thermostats are seeded via
    the API write boundary from the frontend's unit-aware form defaults.

    NOTE: the active unit is not yet resolved inside ``init_db`` on a truly
    fresh DB (the scheduler resolves it afterwards), but a fresh DB has no
    thermostats to seed. An existing install being upgraded already has its
    last-known unit persisted in ``system_settings`` from a prior run, which is
    exactly what we want here.
    """
    sentinel = "migration_eco_defaults_v1"
    async with conn.execute("SELECT value FROM system_settings WHERE key=?", (sentinel,)) as cur:
        if await cur.fetchone():
            return

    unit = await get_system_setting(conn, "temperature_unit", "F")
    defaults = eco.eco_defaults_for_unit(unit)
    cursor = await conn.execute(
        """UPDATE thermostat_configs SET
             eco_cooling_outdoor_threshold=?,
             eco_cooling_full_drift_temp=?,
             eco_cooling_max_drift=?,
             eco_heating_outdoor_threshold=?,
             eco_heating_full_drift_temp=?,
             eco_heating_max_drift=?,
             eco_hysteresis_band=?""",
        (
            defaults["eco_cooling_outdoor_threshold"],
            defaults["eco_cooling_full_drift_temp"],
            defaults["eco_cooling_max_drift"],
            defaults["eco_heating_outdoor_threshold"],
            defaults["eco_heating_full_drift_temp"],
            defaults["eco_heating_max_drift"],
            defaults["eco_hysteresis_band"],
        ),
    )
    if cursor.rowcount and cursor.rowcount > 0:
        log.info(
            "Eco Mode defaults migration: seeded %d existing thermostat(s) with "
            "round-in-%s defaults",
            cursor.rowcount,
            unit,
        )

    await conn.execute(
        """INSERT INTO system_settings(key,value) VALUES(?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (sentinel, "1"),
    )
    await conn.commit()


# Versioned upgrade history, reconstructed from the pre-#21 ad-hoc ALTER list.
# Append-only: never edit or delete an existing entry — create a new version.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "Add temp_offset to rooms",
        ("ALTER TABLE rooms ADD COLUMN temp_offset REAL NOT NULL DEFAULT 0.0",),
    ),
    Migration(
        2,
        "Add name and default_temp to thermostat_configs",
        (
            "ALTER TABLE thermostat_configs ADD COLUMN name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE thermostat_configs ADD COLUMN default_temp REAL",
        ),
    ),
    Migration(
        3,
        "Add reconciliation_interval_min to thermostat_configs",
        (
            "ALTER TABLE thermostat_configs ADD COLUMN reconciliation_interval_min "
            "INTEGER NOT NULL DEFAULT 0",
        ),
    ),
    Migration(
        4,
        "Add control_method to room_vents",
        ("ALTER TABLE room_vents ADD COLUMN control_method TEXT NOT NULL DEFAULT 'open_close'",),
    ),
    # Cycle diagnostics (Issue #60)
    Migration(
        5,
        "Cycle diagnostics columns (Issue #60)",
        (
            "ALTER TABLE cycle_logs ADD COLUMN ended_reason TEXT",
            "ALTER TABLE cycle_logs ADD COLUMN thermostat_temp_at_start REAL",
            "ALTER TABLE cycle_logs ADD COLUMN thermostat_temp_at_end REAL",
            "ALTER TABLE cycle_logs ADD COLUMN setpoint_at_start REAL",
            "ALTER TABLE cycle_logs ADD COLUMN setpoint_at_end REAL",
            "ALTER TABLE cycle_logs ADD COLUMN vents_at_start TEXT",
            "ALTER TABLE cycle_logs ADD COLUMN vents_at_end TEXT",
            "ALTER TABLE room_cycle_states ADD COLUMN temp_at_start REAL",
            "ALTER TABLE room_cycle_states ADD COLUMN temp_at_end REAL",
            "ALTER TABLE room_cycle_states ADD COLUMN trigger_detail TEXT",
            "ALTER TABLE room_cycle_states ADD COLUMN joined_at TEXT",
        ),
    ),
    # Outside-temperature capture (Issue #85 Phase 1c)
    Migration(
        6,
        "Outside-temperature capture on cycle_logs (Issue #85)",
        (
            "ALTER TABLE cycle_logs ADD COLUMN outside_temp_at_start REAL",
            "ALTER TABLE cycle_logs ADD COLUMN outside_temp_at_end REAL",
        ),
    ),
    # Vacation mode thermostat hold strategy
    Migration(
        7,
        "Add vacation_hvac_mode to thermostat_configs",
        (
            "ALTER TABLE thermostat_configs ADD COLUMN vacation_hvac_mode "
            "TEXT NOT NULL DEFAULT 'single'",
        ),
    ),
    # Short-cycle protection (Issue #208)
    Migration(
        8,
        "Short-cycle protection columns (Issue #208)",
        (
            "ALTER TABLE thermostat_configs ADD COLUMN min_cycle_runtime_min "
            "INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE thermostat_configs ADD COLUMN min_cycle_offtime_min "
            "INTEGER NOT NULL DEFAULT 0",
        ),
    ),
    # Outdoor-temperature cooling lockout (Issue #209)
    Migration(
        9,
        "Add cooling_lockout_below_f to thermostat_configs (Issue #209)",
        ("ALTER TABLE thermostat_configs ADD COLUMN cooling_lockout_below_f REAL",),
    ),
    # Airflow-floor / dead-head protection (Issue #213). Replaces the prior
    # count-based ``min_open_vents`` with a fraction-of-total calculation that
    # accounts for passive vents and an optional bypass damper. The legacy
    # column is dropped once the new fields are in place: existing rows had
    # ``min_open_vents`` defaulting to 1, which matches the transitional
    # fallback the engine uses when ``total_vents_count`` is NULL.
    Migration(
        10,
        "Airflow-floor protection, drop legacy min_open_vents (Issue #213)",
        (
            "ALTER TABLE thermostat_configs ADD COLUMN total_vents_count INTEGER",
            "ALTER TABLE thermostat_configs ADD COLUMN has_bypass_damper "
            "INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE thermostat_configs ADD COLUMN min_open_vents_fraction "
            "REAL NOT NULL DEFAULT 0.333",
            "ALTER TABLE thermostat_configs DROP COLUMN min_open_vents",
        ),
    ),
    # Vent-thrashing / overflow conditioning (Issue #237). The hold flag lets
    # the engine remember across ticks that a cycle is past its goal but
    # waiting for min_cycle_runtime_min to elapse — the per-room close-vent
    # loop gates on this to stop reopened vents from flapping back closed.
    Migration(
        11,
        "Overflow conditioning during min-runtime hold (Issue #237)",
        (
            "ALTER TABLE cycle_logs ADD COLUMN in_min_runtime_hold INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE thermostat_configs ADD COLUMN overflow_during_min_runtime "
            "INTEGER NOT NULL DEFAULT 1",
        ),
    ),
    # Overflow-room cycle data points (Issue #254). Non-active rooms opened
    # during the minimum-runtime hold are now recorded as room_cycle_states
    # rows tagged role='overflow' so the Logs page can show their start/end
    # temperatures alongside the cycle that triggered them.
    Migration(
        12,
        "Add role to room_cycle_states (Issue #254)",
        ("ALTER TABLE room_cycle_states ADD COLUMN role TEXT NOT NULL DEFAULT 'active'",),
    ),
    # Ambient-aware presence suppression / pre-cool / pre-heat (Issue #248)
    Migration(
        13,
        "Ambient-aware presence suppression columns (Issue #248)",
        (
            "ALTER TABLE rooms ADD COLUMN ambient_suppression_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE rooms ADD COLUMN ambient_suppression_mode "
            "TEXT NOT NULL DEFAULT 'any_presence'",
            "ALTER TABLE rooms ADD COLUMN ambient_suppression_min_differential "
            "REAL NOT NULL DEFAULT 5.0",
            "ALTER TABLE rooms ADD COLUMN ambient_suppression_deadband REAL NOT NULL DEFAULT 2.0",
            "ALTER TABLE rooms ADD COLUMN ambient_suppression_off_schedule_window_min "
            "INTEGER NOT NULL DEFAULT 60",
        ),
    ),
    # Thermostat-unavailability abort (Issue #267). Minutes of sustained
    # climate-entity unavailability before a running cycle is aborted and all
    # zone vents re-opened. 0 = never abort.
    Migration(
        14,
        "Add unavailable_abort_after_min to thermostat_configs (Issue #267)",
        (
            "ALTER TABLE thermostat_configs ADD COLUMN unavailable_abort_after_min "
            "INTEGER NOT NULL DEFAULT 5",
        ),
    ),
    # Per-room deadband override (Issue #277). NULL = inherit the thermostat's
    # deadband, so existing rooms keep their current behaviour after upgrade.
    Migration(
        15,
        "Add deadband_override to rooms (Issue #277)",
        ("ALTER TABLE rooms ADD COLUMN deadband_override REAL",),
    ),
    # Schedule lifecycle (Issue #359). `enabled` parks a block without deleting
    # it; `expires_at` (naive LOCAL wall-clock ISO, NULL = never) drives the
    # self-disable sweep. Existing rows backfill to enabled=1 / expires_at=NULL,
    # i.e. exactly the pre-#359 behaviour.
    Migration(
        16,
        "Schedule lifecycle columns (Issue #359)",
        (
            "ALTER TABLE schedules ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE schedules ADD COLUMN expires_at TEXT",
        ),
    ),
    # Eco Mode (Issue #404). Per-thermostat config columns backfill existing
    # rows to the round-in-Fahrenheit defaults (eco OFF, so no behaviour change);
    # a °C-mode install rewrites the seven numeric fields once via
    # _migrate_eco_defaults. Per-room columns are all nullable (NULL = inherit).
    # room_cycle_states gains the requested/effective/eco_active measurability
    # columns; existing rows read back eco_active=0 (never relaxed).
    Migration(
        17,
        "Eco Mode config, per-room overrides, and cycle measurability (Issue #404)",
        (
            "ALTER TABLE thermostat_configs ADD COLUMN eco_mode_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE thermostat_configs ADD COLUMN eco_cooling_outdoor_threshold "
            "REAL NOT NULL DEFAULT 86.0",
            "ALTER TABLE thermostat_configs ADD COLUMN eco_cooling_full_drift_temp "
            "REAL NOT NULL DEFAULT 100.0",
            "ALTER TABLE thermostat_configs ADD COLUMN eco_cooling_max_drift "
            "REAL NOT NULL DEFAULT 4.0",
            "ALTER TABLE thermostat_configs ADD COLUMN eco_heating_outdoor_threshold "
            "REAL NOT NULL DEFAULT 40.0",
            "ALTER TABLE thermostat_configs ADD COLUMN eco_heating_full_drift_temp "
            "REAL NOT NULL DEFAULT 0.0",
            "ALTER TABLE thermostat_configs ADD COLUMN eco_heating_max_drift "
            "REAL NOT NULL DEFAULT 4.0",
            "ALTER TABLE thermostat_configs ADD COLUMN eco_hysteresis_band "
            "REAL NOT NULL DEFAULT 2.0",
            "ALTER TABLE rooms ADD COLUMN eco_mode_enabled INTEGER",
            "ALTER TABLE rooms ADD COLUMN eco_cooling_outdoor_threshold REAL",
            "ALTER TABLE rooms ADD COLUMN eco_cooling_full_drift_temp REAL",
            "ALTER TABLE rooms ADD COLUMN eco_cooling_max_drift REAL",
            "ALTER TABLE rooms ADD COLUMN eco_heating_outdoor_threshold REAL",
            "ALTER TABLE rooms ADD COLUMN eco_heating_full_drift_temp REAL",
            "ALTER TABLE rooms ADD COLUMN eco_heating_max_drift REAL",
            "ALTER TABLE rooms ADD COLUMN eco_hysteresis_band REAL",
            "ALTER TABLE room_cycle_states ADD COLUMN requested_target REAL",
            "ALTER TABLE room_cycle_states ADD COLUMN effective_target REAL",
            "ALTER TABLE room_cycle_states ADD COLUMN eco_active INTEGER NOT NULL DEFAULT 0",
        ),
    ),
    # Per-schedule deadband override (Issue #517). NULL = inherit the room's
    # deadband_override, then the thermostat's deadband — so every existing
    # block keeps its current behaviour after upgrade. Touches `schedules`
    # only, so (like migration 16) it stays baseline for a legacy `rooms`
    # fixture rather than bumping _NEWEST_LEGACY_ROOMS_VERSION.
    Migration(
        18,
        "Add deadband_override to schedules (Issue #517)",
        ("ALTER TABLE schedules ADD COLUMN deadband_override REAL",),
    ),
    # Optional schedule display name (Issue #520). NULL = unnamed, which is what
    # every existing block backfills to — callers fall back to `id`, exactly the
    # behaviour before the column existed. Touches `schedules` only, so (like
    # migrations 16 and 18) it stays baseline for a legacy `rooms` fixture
    # rather than bumping _NEWEST_LEGACY_ROOMS_VERSION.
    Migration(
        19,
        "Add name to schedules (Issue #520)",
        ("ALTER TABLE schedules ADD COLUMN name TEXT",),
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dt(s: str | None) -> datetime | None:
    """Read a datetime string from the DB as UTC-aware."""
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _dt_required(s: str | None) -> datetime:
    """Like _dt but asserts the value is non-null (for NOT NULL DB columns)."""
    result = _dt(s)
    assert result is not None, f"Expected non-null datetime from DB, got: {s!r}"
    return result


def _t(s: str) -> time:
    return time.fromisoformat(s)


def _dts(dt: datetime | None) -> str | None:
    """Write a datetime to the DB as a naive UTC string."""
    return dt.replace(tzinfo=None).isoformat() if dt else None


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------


async def get_all_rooms(conn: aiosqlite.Connection) -> list[Room]:
    async with conn.execute("SELECT * FROM rooms ORDER BY name") as cur:
        rows = await cur.fetchall()
    return [_row_to_room(r) for r in rows]


async def get_room(conn: aiosqlite.Connection, room_id: str) -> Room | None:
    async with conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_room(row) if row else None


async def get_rooms_for_thermostat(
    conn: aiosqlite.Connection, thermostat_entity_id: str
) -> list[Room]:
    async with conn.execute(
        "SELECT * FROM rooms WHERE thermostat_entity_id=? ORDER BY name",
        (thermostat_entity_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_room(r) for r in rows]


def _row_to_room(row) -> Room:
    return Room(
        id=row["id"],
        name=row["name"],
        thermostat_entity_id=row["thermostat_entity_id"],
        include_thermostat_sensor=bool(row["include_thermostat_sensor"]),
        system_wide_temp=row["system_wide_temp"],
        presence_holdover_hours=row["presence_holdover_hours"],
        notes=row["notes"],
        temp_offset=row["temp_offset"] if row["temp_offset"] is not None else 0.0,
        deadband_override=row["deadband_override"],
        ambient_suppression_enabled=bool(row["ambient_suppression_enabled"]),
        ambient_suppression_mode=row["ambient_suppression_mode"],
        ambient_suppression_min_differential=row["ambient_suppression_min_differential"],
        ambient_suppression_deadband=row["ambient_suppression_deadband"],
        ambient_suppression_off_schedule_window_min=row[
            "ambient_suppression_off_schedule_window_min"
        ],
        # Eco Mode per-room overrides (Issue #404) — all nullable (NULL = inherit
        # the thermostat value). eco_mode_enabled is a tri-state: NULL inherits.
        eco_mode_enabled=(
            None if row["eco_mode_enabled"] is None else bool(row["eco_mode_enabled"])
        ),
        eco_cooling_outdoor_threshold=row["eco_cooling_outdoor_threshold"],
        eco_cooling_full_drift_temp=row["eco_cooling_full_drift_temp"],
        eco_cooling_max_drift=row["eco_cooling_max_drift"],
        eco_heating_outdoor_threshold=row["eco_heating_outdoor_threshold"],
        eco_heating_full_drift_temp=row["eco_heating_full_drift_temp"],
        eco_heating_max_drift=row["eco_heating_max_drift"],
        eco_hysteresis_band=row["eco_hysteresis_band"],
    )


async def upsert_room(conn: aiosqlite.Connection, room: Room) -> None:
    await conn.execute(
        """INSERT INTO rooms (id,name,thermostat_entity_id,include_thermostat_sensor,
           system_wide_temp,presence_holdover_hours,notes,temp_offset,deadband_override,
           ambient_suppression_enabled,ambient_suppression_mode,
           ambient_suppression_min_differential,ambient_suppression_deadband,
           ambient_suppression_off_schedule_window_min,
           eco_mode_enabled,eco_cooling_outdoor_threshold,eco_cooling_full_drift_temp,
           eco_cooling_max_drift,eco_heating_outdoor_threshold,eco_heating_full_drift_temp,
           eco_heating_max_drift,eco_hysteresis_band)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name,
             thermostat_entity_id=excluded.thermostat_entity_id,
             include_thermostat_sensor=excluded.include_thermostat_sensor,
             system_wide_temp=excluded.system_wide_temp,
             presence_holdover_hours=excluded.presence_holdover_hours,
             notes=excluded.notes,
             temp_offset=excluded.temp_offset,
             deadband_override=excluded.deadband_override,
             ambient_suppression_enabled=excluded.ambient_suppression_enabled,
             ambient_suppression_mode=excluded.ambient_suppression_mode,
             ambient_suppression_min_differential=excluded.ambient_suppression_min_differential,
             ambient_suppression_deadband=excluded.ambient_suppression_deadband,
             ambient_suppression_off_schedule_window_min=excluded.ambient_suppression_off_schedule_window_min,
             eco_mode_enabled=excluded.eco_mode_enabled,
             eco_cooling_outdoor_threshold=excluded.eco_cooling_outdoor_threshold,
             eco_cooling_full_drift_temp=excluded.eco_cooling_full_drift_temp,
             eco_cooling_max_drift=excluded.eco_cooling_max_drift,
             eco_heating_outdoor_threshold=excluded.eco_heating_outdoor_threshold,
             eco_heating_full_drift_temp=excluded.eco_heating_full_drift_temp,
             eco_heating_max_drift=excluded.eco_heating_max_drift,
             eco_hysteresis_band=excluded.eco_hysteresis_band
        """,
        (
            room.id,
            room.name,
            room.thermostat_entity_id,
            int(room.include_thermostat_sensor),
            room.system_wide_temp,
            room.presence_holdover_hours,
            room.notes,
            room.temp_offset,
            room.deadband_override,
            int(room.ambient_suppression_enabled),
            room.ambient_suppression_mode,
            room.ambient_suppression_min_differential,
            room.ambient_suppression_deadband,
            room.ambient_suppression_off_schedule_window_min,
            None if room.eco_mode_enabled is None else int(room.eco_mode_enabled),
            room.eco_cooling_outdoor_threshold,
            room.eco_cooling_full_drift_temp,
            room.eco_cooling_max_drift,
            room.eco_heating_outdoor_threshold,
            room.eco_heating_full_drift_temp,
            room.eco_heating_max_drift,
            room.eco_hysteresis_band,
        ),
    )
    await conn.commit()


async def delete_room(conn: aiosqlite.Connection, room_id: str) -> None:
    await conn.execute("DELETE FROM rooms WHERE id=?", (room_id,))
    await conn.commit()


# ---------------------------------------------------------------------------
# Room sub-entities (sensors, vents, presence)
# ---------------------------------------------------------------------------


async def get_room_sensors(conn: aiosqlite.Connection, room_id: str) -> list[RoomSensor]:
    async with conn.execute("SELECT * FROM room_sensors WHERE room_id=?", (room_id,)) as cur:
        rows = await cur.fetchall()
    return [RoomSensor(id=r["id"], room_id=r["room_id"], entity_id=r["entity_id"]) for r in rows]


async def add_room_sensor(conn: aiosqlite.Connection, s: RoomSensor) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO room_sensors(id,room_id,entity_id) VALUES (?,?,?)",
        (s.id, s.room_id, s.entity_id),
    )
    await conn.commit()


async def remove_room_sensor(conn: aiosqlite.Connection, room_id: str, entity_id: str) -> None:
    await conn.execute(
        "DELETE FROM room_sensors WHERE room_id=? AND entity_id=?", (room_id, entity_id)
    )
    await conn.commit()


async def get_room_vents(conn: aiosqlite.Connection, room_id: str) -> list[RoomVent]:
    async with conn.execute("SELECT * FROM room_vents WHERE room_id=?", (room_id,)) as cur:
        rows = await cur.fetchall()
    return [
        RoomVent(
            id=r["id"],
            room_id=r["room_id"],
            entity_id=r["entity_id"],
            control_method=r["control_method"],
        )
        for r in rows
    ]


async def add_room_vent(conn: aiosqlite.Connection, v: RoomVent) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO room_vents(id,room_id,entity_id,control_method) VALUES (?,?,?,?)",
        (v.id, v.room_id, v.entity_id, v.control_method),
    )
    await conn.commit()


async def update_room_vent_control_method(
    conn: aiosqlite.Connection,
    room_id: str,
    entity_id: str,
    control_method: str,
) -> None:
    await conn.execute(
        "UPDATE room_vents SET control_method=? WHERE room_id=? AND entity_id=?",
        (control_method, room_id, entity_id),
    )
    await conn.commit()


async def remove_room_vent(conn: aiosqlite.Connection, room_id: str, entity_id: str) -> None:
    await conn.execute(
        "DELETE FROM room_vents WHERE room_id=? AND entity_id=?", (room_id, entity_id)
    )
    await conn.commit()


async def get_room_presence_sensors(
    conn: aiosqlite.Connection, room_id: str
) -> list[RoomPresenceSensor]:
    async with conn.execute(
        "SELECT * FROM room_presence_sensors WHERE room_id=?", (room_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [
        RoomPresenceSensor(id=r["id"], room_id=r["room_id"], entity_id=r["entity_id"]) for r in rows
    ]


async def add_room_presence_sensor(conn: aiosqlite.Connection, p: RoomPresenceSensor) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO room_presence_sensors(id,room_id,entity_id) VALUES (?,?,?)",
        (p.id, p.room_id, p.entity_id),
    )
    await conn.commit()


async def remove_room_presence_sensor(
    conn: aiosqlite.Connection, room_id: str, entity_id: str
) -> None:
    await conn.execute(
        "DELETE FROM room_presence_sensors WHERE room_id=? AND entity_id=?",
        (room_id, entity_id),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


async def get_schedules_for_room(conn: aiosqlite.Connection, room_id: str) -> list[Schedule]:
    async with conn.execute(
        "SELECT * FROM schedules WHERE room_id=? ORDER BY start_time", (room_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_schedule(r) for r in rows]


async def get_all_schedules(conn: aiosqlite.Connection) -> list[Schedule]:
    async with conn.execute("SELECT * FROM schedules") as cur:
        rows = await cur.fetchall()
    return [_row_to_schedule(r) for r in rows]


async def get_expiring_schedules(conn: aiosqlite.Connection) -> list[Schedule]:
    """Enabled schedules that carry an expiry — candidates for the self-disable
    sweep (Issue #359). Disabled rows and never-expire rows are excluded."""
    async with conn.execute(
        "SELECT * FROM schedules WHERE enabled=1 AND expires_at IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_schedule(r) for r in rows]


def _row_to_schedule(row) -> Schedule:
    # `enabled` / `expires_at` always exist post-migration (init_db runs the
    # ALTER TABLEs before any read). `expires_at` is a naive LOCAL wall-clock
    # ISO string (matching start_time/end_time), so parse it directly — do NOT
    # use _dt(), which treats naive values as UTC (Issue #359).
    raw_expires = row["expires_at"]
    return Schedule(
        id=row["id"],
        room_id=row["room_id"],
        days_of_week=json.loads(row["days_of_week"]),
        start_time=_t(row["start_time"]),
        end_time=_t(row["end_time"]),
        target_temp=row["target_temp"],
        enabled=bool(row["enabled"]),
        expires_at=datetime.fromisoformat(raw_expires) if raw_expires else None,
        deadband_override=row["deadband_override"],
        # NULL for every pre-#520 row, and for any block the user never named.
        name=row["name"],
    )


async def upsert_schedule(conn: aiosqlite.Connection, s: Schedule) -> None:
    # expires_at: persist as naive LOCAL wall-clock ISO (strip tzinfo if an
    # aware datetime slips in) so it round-trips with start_time/end_time.
    expires_iso = (
        s.expires_at.replace(tzinfo=None).isoformat() if s.expires_at is not None else None
    )
    await conn.execute(
        """INSERT INTO schedules(
               id,room_id,days_of_week,start_time,end_time,target_temp,enabled,expires_at,
               deadband_override,name)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             days_of_week=excluded.days_of_week,
             start_time=excluded.start_time,
             end_time=excluded.end_time,
             target_temp=excluded.target_temp,
             enabled=excluded.enabled,
             expires_at=excluded.expires_at,
             deadband_override=excluded.deadband_override,
             name=excluded.name
        """,
        (
            s.id,
            s.room_id,
            json.dumps(s.days_of_week),
            s.start_time.isoformat(),
            s.end_time.isoformat(),
            s.target_temp,
            1 if s.enabled else 0,
            expires_iso,
            s.deadband_override,
            s.name,
        ),
    )
    await conn.commit()


async def delete_schedule(conn: aiosqlite.Connection, schedule_id: str) -> None:
    await conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
    await conn.commit()


# ---------------------------------------------------------------------------
# Thermostat config
# ---------------------------------------------------------------------------


async def get_thermostat_config(conn: aiosqlite.Connection, entity_id: str) -> ThermostatConfig:
    async with conn.execute(
        "SELECT * FROM thermostat_configs WHERE thermostat_entity_id=?", (entity_id,)
    ) as cur:
        row = await cur.fetchone()
    if row:
        return _row_to_tc(row)
    return ThermostatConfig(thermostat_entity_id=entity_id)


async def get_all_thermostat_configs(
    conn: aiosqlite.Connection,
) -> list[ThermostatConfig]:
    async with conn.execute("SELECT * FROM thermostat_configs") as cur:
        rows = await cur.fetchall()
    return [_row_to_tc(r) for r in rows]


def _row_to_tc(row) -> ThermostatConfig:
    keys = row.keys() if hasattr(row, "keys") else []
    return ThermostatConfig(
        thermostat_entity_id=row["thermostat_entity_id"],
        name=row["name"] if row["name"] is not None else "",
        default_temp=row["default_temp"],
        min_setpoint=row["min_setpoint"],
        max_setpoint=row["max_setpoint"],
        deadband=row["deadband"],
        max_vent_closed_min=row["max_vent_closed_min"],
        overshoot_delta=row["overshoot_delta"],
        cycle_timeout_hours=row["cycle_timeout_hours"],
        reconciliation_interval_min=int(row["reconciliation_interval_min"] or 0),
        vacation_hvac_mode=row["vacation_hvac_mode"] if "vacation_hvac_mode" in keys else "single",
        min_cycle_runtime_min=int(row["min_cycle_runtime_min"] or 0)
        if "min_cycle_runtime_min" in keys
        else 0,
        min_cycle_offtime_min=int(row["min_cycle_offtime_min"] or 0)
        if "min_cycle_offtime_min" in keys
        else 0,
        cooling_lockout_below_f=row["cooling_lockout_below_f"]
        if "cooling_lockout_below_f" in keys
        else None,
        total_vents_count=row["total_vents_count"] if "total_vents_count" in keys else None,
        has_bypass_damper=bool(row["has_bypass_damper"]) if "has_bypass_damper" in keys else False,
        min_open_vents_fraction=row["min_open_vents_fraction"]
        if "min_open_vents_fraction" in keys
        else 0.333,
        overflow_during_min_runtime=bool(row["overflow_during_min_runtime"])
        if "overflow_during_min_runtime" in keys
        else True,
        unavailable_abort_after_min=int(row["unavailable_abort_after_min"])
        if "unavailable_abort_after_min" in keys and row["unavailable_abort_after_min"] is not None
        else 5,
        eco_mode_enabled=bool(row["eco_mode_enabled"]) if "eco_mode_enabled" in keys else False,
        # Explicit so the eco-field splat below type-checks against only the
        # float-typed eco params (eco_suspend_until is API-layer-populated,
        # never a thermostat_configs column).
        eco_suspend_until=None,
        **{
            field: (
                row[field]
                if field in keys and row[field] is not None
                else eco.ECO_DEFAULTS_F[field]
            )
            for field in eco.ECO_TEMP_FIELDS
        },
    )


async def upsert_thermostat_config(conn: aiosqlite.Connection, tc: ThermostatConfig) -> None:
    await conn.execute(
        """INSERT INTO thermostat_configs
           (thermostat_entity_id,name,default_temp,min_setpoint,max_setpoint,deadband,
            max_vent_closed_min,overshoot_delta,cycle_timeout_hours,
            reconciliation_interval_min,vacation_hvac_mode,
            min_cycle_runtime_min,min_cycle_offtime_min,cooling_lockout_below_f,
            total_vents_count,has_bypass_damper,min_open_vents_fraction,
            overflow_during_min_runtime,unavailable_abort_after_min,
            eco_mode_enabled,eco_cooling_outdoor_threshold,eco_cooling_full_drift_temp,
            eco_cooling_max_drift,eco_heating_outdoor_threshold,eco_heating_full_drift_temp,
            eco_heating_max_drift,eco_hysteresis_band)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(thermostat_entity_id) DO UPDATE SET
             name=excluded.name,
             default_temp=excluded.default_temp,
             min_setpoint=excluded.min_setpoint,
             max_setpoint=excluded.max_setpoint,
             deadband=excluded.deadband,
             max_vent_closed_min=excluded.max_vent_closed_min,
             overshoot_delta=excluded.overshoot_delta,
             cycle_timeout_hours=excluded.cycle_timeout_hours,
             reconciliation_interval_min=excluded.reconciliation_interval_min,
             vacation_hvac_mode=excluded.vacation_hvac_mode,
             min_cycle_runtime_min=excluded.min_cycle_runtime_min,
             min_cycle_offtime_min=excluded.min_cycle_offtime_min,
             cooling_lockout_below_f=excluded.cooling_lockout_below_f,
             total_vents_count=excluded.total_vents_count,
             has_bypass_damper=excluded.has_bypass_damper,
             min_open_vents_fraction=excluded.min_open_vents_fraction,
             overflow_during_min_runtime=excluded.overflow_during_min_runtime,
             unavailable_abort_after_min=excluded.unavailable_abort_after_min,
             eco_mode_enabled=excluded.eco_mode_enabled,
             eco_cooling_outdoor_threshold=excluded.eco_cooling_outdoor_threshold,
             eco_cooling_full_drift_temp=excluded.eco_cooling_full_drift_temp,
             eco_cooling_max_drift=excluded.eco_cooling_max_drift,
             eco_heating_outdoor_threshold=excluded.eco_heating_outdoor_threshold,
             eco_heating_full_drift_temp=excluded.eco_heating_full_drift_temp,
             eco_heating_max_drift=excluded.eco_heating_max_drift,
             eco_hysteresis_band=excluded.eco_hysteresis_band
        """,
        (
            tc.thermostat_entity_id,
            tc.name,
            tc.default_temp,
            tc.min_setpoint,
            tc.max_setpoint,
            tc.deadband,
            tc.max_vent_closed_min,
            tc.overshoot_delta,
            tc.cycle_timeout_hours,
            tc.reconciliation_interval_min,
            tc.vacation_hvac_mode,
            tc.min_cycle_runtime_min,
            tc.min_cycle_offtime_min,
            tc.cooling_lockout_below_f,
            tc.total_vents_count,
            int(tc.has_bypass_damper),
            tc.min_open_vents_fraction,
            int(tc.overflow_during_min_runtime),
            tc.unavailable_abort_after_min,
            int(tc.eco_mode_enabled),
            tc.eco_cooling_outdoor_threshold,
            tc.eco_cooling_full_drift_temp,
            tc.eco_cooling_max_drift,
            tc.eco_heating_outdoor_threshold,
            tc.eco_heating_full_drift_temp,
            tc.eco_heating_max_drift,
            tc.eco_hysteresis_band,
        ),
    )
    await conn.commit()


async def delete_thermostat_config(conn: aiosqlite.Connection, entity_id: str) -> None:
    await conn.execute("DELETE FROM thermostat_configs WHERE thermostat_entity_id=?", (entity_id,))
    # A deleted thermostat takes its Eco suspension with it (Issue #500).
    await conn.execute("DELETE FROM eco_suspensions WHERE thermostat_entity_id=?", (entity_id,))
    await conn.commit()


# ---------------------------------------------------------------------------
# Eco Suspend (Issue #500)
# ---------------------------------------------------------------------------


async def get_all_eco_suspensions(conn: aiosqlite.Connection) -> dict[str, datetime]:
    """All active suspensions as {thermostat_entity_id: resume_at (UTC-aware)}."""
    async with conn.execute("SELECT thermostat_entity_id, resume_at FROM eco_suspensions") as cur:
        rows = await cur.fetchall()
    return {r["thermostat_entity_id"]: _dt_required(r["resume_at"]) for r in rows}


async def set_eco_suspension(
    conn: aiosqlite.Connection, entity_id: str, resume_at: datetime
) -> None:
    await conn.execute(
        """INSERT INTO eco_suspensions(thermostat_entity_id, resume_at) VALUES(?,?)
           ON CONFLICT(thermostat_entity_id) DO UPDATE SET resume_at=excluded.resume_at""",
        (entity_id, _dts(resume_at)),
    )
    await conn.commit()


async def delete_eco_suspension(conn: aiosqlite.Connection, entity_id: str) -> None:
    await conn.execute("DELETE FROM eco_suspensions WHERE thermostat_entity_id=?", (entity_id,))
    await conn.commit()


async def thermostat_config_exists(conn: aiosqlite.Connection, entity_id: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM thermostat_configs WHERE thermostat_entity_id=?", (entity_id,)
    ) as cur:
        return await cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Room overrides
# ---------------------------------------------------------------------------


async def get_room_override(conn: aiosqlite.Connection, room_id: str) -> RoomOverride | None:
    async with conn.execute("SELECT * FROM room_overrides WHERE room_id=?", (room_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return RoomOverride(
        room_id=row["room_id"],
        target_temp=row["target_temp"],
        expires_at=_dt_required(row["expires_at"]),
    )


async def set_room_override(conn: aiosqlite.Connection, override: RoomOverride) -> None:
    await conn.execute(
        """INSERT INTO room_overrides(room_id,target_temp,expires_at) VALUES(?,?,?)
           ON CONFLICT(room_id) DO UPDATE SET
             target_temp=excluded.target_temp, expires_at=excluded.expires_at
        """,
        (
            override.room_id,
            override.target_temp,
            override.expires_at.replace(tzinfo=None).isoformat(),
        ),
    )
    await conn.commit()


async def clear_room_override(conn: aiosqlite.Connection, room_id: str) -> None:
    await conn.execute("DELETE FROM room_overrides WHERE room_id=?", (room_id,))
    await conn.commit()


async def clear_expired_overrides(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        "DELETE FROM room_overrides WHERE expires_at < ?",
        (datetime.now(UTC).replace(tzinfo=None).isoformat(),),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# Presence holdover state
# ---------------------------------------------------------------------------


async def get_all_holdover_states(
    conn: aiosqlite.Connection,
) -> list[PresenceHoldoverState]:
    async with conn.execute("SELECT * FROM presence_holdover_state") as cur:
        rows = await cur.fetchall()
    return [
        PresenceHoldoverState(
            room_id=r["room_id"],
            last_detected_at=_dt_required(r["last_detected_at"]),
            expires_at=_dt_required(r["expires_at"]),
        )
        for r in rows
    ]


async def get_holdover_state(
    conn: aiosqlite.Connection, room_id: str
) -> PresenceHoldoverState | None:
    async with conn.execute(
        "SELECT * FROM presence_holdover_state WHERE room_id=?", (room_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return PresenceHoldoverState(
        room_id=row["room_id"],
        last_detected_at=_dt_required(row["last_detected_at"]),
        expires_at=_dt_required(row["expires_at"]),
    )


async def upsert_holdover_state(conn: aiosqlite.Connection, state: PresenceHoldoverState) -> None:
    await conn.execute(
        """INSERT INTO presence_holdover_state(room_id,last_detected_at,expires_at)
           VALUES(?,?,?)
           ON CONFLICT(room_id) DO UPDATE SET
             last_detected_at=excluded.last_detected_at,
             expires_at=excluded.expires_at
        """,
        (
            state.room_id,
            state.last_detected_at.replace(tzinfo=None).isoformat(),
            state.expires_at.replace(tzinfo=None).isoformat(),
        ),
    )
    await conn.commit()


async def delete_holdover_state(conn: aiosqlite.Connection, room_id: str) -> None:
    await conn.execute("DELETE FROM presence_holdover_state WHERE room_id=?", (room_id,))
    await conn.commit()


async def set_presence_suppression(
    conn: aiosqlite.Connection, room_id: str, cleared_at: datetime
) -> None:
    """Mark a room's presence as user-cleared (#439).

    While the marker exists, the continuous-presence refresh and sensor
    on-edges must not write a holdover for the room. Idempotent — repeated
    clears just refresh the timestamp.
    """
    await conn.execute(
        """INSERT INTO presence_suppression(room_id, cleared_at)
           VALUES(?,?)
           ON CONFLICT(room_id) DO UPDATE SET cleared_at=excluded.cleared_at
        """,
        (room_id, cleared_at.replace(tzinfo=None).isoformat()),
    )
    await conn.commit()


async def is_presence_suppressed(conn: aiosqlite.Connection, room_id: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM presence_suppression WHERE room_id=?", (room_id,)
    ) as cur:
        return await cur.fetchone() is not None


async def delete_presence_suppression(conn: aiosqlite.Connection, room_id: str) -> None:
    await conn.execute("DELETE FROM presence_suppression WHERE room_id=?", (room_id,))
    await conn.commit()


# ---------------------------------------------------------------------------
# Cycle logs
# ---------------------------------------------------------------------------


async def close_open_cycle_logs(
    conn: aiosqlite.Connection,
    thermostat_entity_id: str,
    ended_at: datetime | None = None,
) -> int:
    """
    Close all open (ended_at IS NULL) cycle logs for a thermostat.

    Called before starting a new cycle so that orphaned rows from a previous
    server run (or an exception-path that left a dangling open log) do not
    accumulate. Returns the number of rows closed.
    """
    if ended_at is None:
        ended_at = datetime.now(UTC)
    async with conn.execute(
        "UPDATE cycle_logs SET ended_at=? WHERE thermostat_entity_id=? AND ended_at IS NULL",
        (ended_at.replace(tzinfo=None).isoformat(), thermostat_entity_id),
    ) as cur:
        count = cur.rowcount or 0
    await conn.commit()
    return count


async def close_open_cycles_for_rooms(
    conn: aiosqlite.Connection,
    room_ids: list[str],
    exclude_thermostat: str | None = None,
    ended_at: datetime | None = None,
) -> int:
    """
    Close any open cycle logs that contain one or more of the given room IDs.

    Used to prevent the same room from appearing in two simultaneous cycles
    (e.g. after a room is reassigned between thermostats).  The caller's own
    thermostat can be excluded since its orphans are already cleaned up by
    close_open_cycle_logs().  Returns the number of cycle logs closed.
    (Issue #48 Bug 4)
    """
    if not room_ids:
        return 0
    if ended_at is None:
        ended_at = datetime.now(UTC)
    placeholders = ",".join("?" for _ in room_ids)
    query = (
        "UPDATE cycle_logs SET ended_at=? "
        "WHERE ended_at IS NULL "
        "AND id IN ("
        f"  SELECT DISTINCT cl.id FROM cycle_logs cl "
        f"  JOIN room_cycle_states rcs ON rcs.cycle_id = cl.id "
        # Overflow rooms (Issue #254) are not active targets — deleting/moving
        # one must not close an otherwise-running cycle.
        f"  WHERE cl.ended_at IS NULL AND rcs.role != 'overflow' "
        f"  AND rcs.room_id IN ({placeholders})"
    )
    params: list = [ended_at.replace(tzinfo=None).isoformat()]
    params.extend(room_ids)
    if exclude_thermostat:
        query += " AND cl.thermostat_entity_id != ?"
        params.append(exclude_thermostat)
    query += ")"
    async with conn.execute(query, params) as cur:
        count = cur.rowcount or 0
    await conn.commit()
    return count


def _row_to_cycle_log(r) -> CycleLog:
    keys = r.keys() if hasattr(r, "keys") else []

    def _get(name: str):
        return r[name] if name in keys else None

    return CycleLog(
        id=r["id"],
        thermostat_entity_id=r["thermostat_entity_id"],
        started_at=_dt_required(r["started_at"]),
        mode=r["mode"],
        rooms_json=r["rooms_json"],
        ended_at=_dt(r["ended_at"]),
        ended_reason=_get("ended_reason"),
        thermostat_temp_at_start=_get("thermostat_temp_at_start"),
        thermostat_temp_at_end=_get("thermostat_temp_at_end"),
        setpoint_at_start=_get("setpoint_at_start"),
        setpoint_at_end=_get("setpoint_at_end"),
        vents_at_start=_get("vents_at_start"),
        vents_at_end=_get("vents_at_end"),
        outside_temp_at_start=_get("outside_temp_at_start"),
        outside_temp_at_end=_get("outside_temp_at_end"),
        in_min_runtime_hold=bool(_get("in_min_runtime_hold") or 0),
    )


async def set_cycle_log_min_runtime_hold(
    conn: aiosqlite.Connection, cycle_id: str, in_hold: bool
) -> None:
    """Persist the minimum-runtime hold flag on a running cycle (Issue #237)."""
    await conn.execute(
        "UPDATE cycle_logs SET in_min_runtime_hold=? WHERE id=?",
        (1 if in_hold else 0, cycle_id),
    )
    await conn.commit()


async def get_latest_cycle_end(
    conn: aiosqlite.Connection, thermostat_entity_id: str
) -> datetime | None:
    """When this thermostat's most recent CLOSED cycle ended, or None (#432).

    Used to rehydrate the compressor off-time lockout clock across a restart —
    both normal termination and abort write ``ended_at``.
    """
    async with conn.execute(
        "SELECT ended_at FROM cycle_logs WHERE thermostat_entity_id=? "
        "AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1",
        (thermostat_entity_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return _dt(row[0])


async def get_open_cycle_logs(
    conn: aiosqlite.Connection,
    thermostat_entity_id: str,
) -> list[CycleLog]:
    """Return all open (ended_at IS NULL) cycle logs for a thermostat, newest first."""
    async with conn.execute(
        "SELECT * FROM cycle_logs WHERE thermostat_entity_id=? AND ended_at IS NULL ORDER BY started_at DESC",
        (thermostat_entity_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_cycle_log(r) for r in rows]


async def get_cycle_log(conn: aiosqlite.Connection, cycle_id: str) -> CycleLog | None:
    async with conn.execute("SELECT * FROM cycle_logs WHERE id=?", (cycle_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_cycle_log(row) if row else None


async def insert_cycle_log(conn: aiosqlite.Connection, log_: CycleLog) -> None:
    await conn.execute(
        """INSERT INTO cycle_logs(
            id, thermostat_entity_id, started_at, ended_at, mode, rooms_json,
            ended_reason, thermostat_temp_at_start, setpoint_at_start, vents_at_start,
            outside_temp_at_start
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            log_.id,
            log_.thermostat_entity_id,
            log_.started_at.replace(tzinfo=None).isoformat(),
            _dts(log_.ended_at),
            log_.mode,
            log_.rooms_json,
            log_.ended_reason,
            log_.thermostat_temp_at_start,
            log_.setpoint_at_start,
            log_.vents_at_start,
            log_.outside_temp_at_start,
        ),
    )
    await conn.commit()


async def update_cycle_log_rooms(
    conn: aiosqlite.Connection, cycle_id: str, rooms_json: str
) -> None:
    """Refresh an open cycle's room snapshot after a mid-cycle change (#215).

    The room set or a room's trigger (source / target_temp) can change while a
    cycle runs; keeping ``rooms_json`` current means /api/logs reflects reality
    rather than the state captured at cycle start.
    """
    await conn.execute(
        "UPDATE cycle_logs SET rooms_json=? WHERE id=?",
        (rooms_json, cycle_id),
    )
    await conn.commit()


async def close_cycle_log(
    conn: aiosqlite.Connection,
    cycle_id: str,
    ended_at: datetime,
    ended_reason: str | None = None,
    thermostat_temp_at_end: float | None = None,
    setpoint_at_end: float | None = None,
    vents_at_end: str | None = None,
    outside_temp_at_end: float | None = None,
) -> None:
    await conn.execute(
        """UPDATE cycle_logs SET
            ended_at=?,
            ended_reason=COALESCE(?, ended_reason),
            thermostat_temp_at_end=COALESCE(?, thermostat_temp_at_end),
            setpoint_at_end=COALESCE(?, setpoint_at_end),
            vents_at_end=COALESCE(?, vents_at_end),
            outside_temp_at_end=COALESCE(?, outside_temp_at_end)
           WHERE id=?""",
        (
            ended_at.replace(tzinfo=None).isoformat(),
            ended_reason,
            thermostat_temp_at_end,
            setpoint_at_end,
            vents_at_end,
            outside_temp_at_end,
            cycle_id,
        ),
    )
    await conn.commit()


async def get_cycle_logs(
    conn: aiosqlite.Connection,
    limit: int = 50,
    offset: int = 0,
    since: str | None = None,
    until: str | None = None,
) -> list[CycleLog]:
    conditions: list[str] = []
    params: list = []
    if since:
        conditions.append("started_at >= ?")
        params.append(since)
    if until:
        conditions.append("started_at <= ?")
        params.append(until)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params += [limit, offset]
    async with conn.execute(
        f"SELECT * FROM cycle_logs {where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_cycle_log(r) for r in rows]


async def purge_cycle_logs(conn: aiosqlite.Connection, older_than_days: int) -> int:
    """Delete cycle logs older than N days. Returns number of rows deleted.

    ``demo-`` prefixed rows (the deterministic demo dataset, Issue #442) are
    exempt: they live in a fixed past window that retention would otherwise
    delete on the next purge pass, and they are wiped/rewritten wholesale by
    ``demo_seed.seed_demo_metrics`` instead.
    """
    cutoff = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=older_than_days)).isoformat()
    async with conn.execute(
        "DELETE FROM cycle_logs WHERE started_at < ? AND id NOT LIKE 'demo-%'", (cutoff,)
    ) as cur:
        count = cur.rowcount or 0
    await conn.commit()
    return count


async def upsert_room_cycle_state(conn: aiosqlite.Connection, rcs: RoomCycleState) -> None:
    await conn.execute(
        """INSERT INTO room_cycle_states(
            cycle_id, room_id, target_temp, reached_at, vent_closed_at,
            temp_at_start, temp_at_end, trigger_detail, joined_at, role,
            requested_target, effective_target, eco_active
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(cycle_id,room_id) DO UPDATE SET
             target_temp=excluded.target_temp,
             reached_at=excluded.reached_at,
             vent_closed_at=excluded.vent_closed_at,
             temp_at_end=excluded.temp_at_end,
             trigger_detail=COALESCE(excluded.trigger_detail, room_cycle_states.trigger_detail),
             role=excluded.role,
             joined_at=COALESCE(room_cycle_states.joined_at, excluded.joined_at),
             requested_target=excluded.requested_target,
             effective_target=excluded.effective_target,
             eco_active=excluded.eco_active
        """,
        (
            rcs.cycle_id,
            rcs.room_id,
            rcs.target_temp,
            _dts(rcs.reached_at),
            _dts(rcs.vent_closed_at),
            rcs.temp_at_start,
            rcs.temp_at_end,
            rcs.trigger_detail,
            _dts(rcs.joined_at),
            rcs.role,
            rcs.requested_target,
            rcs.effective_target,
            int(rcs.eco_active),
        ),
    )
    await conn.commit()


def _row_to_room_cycle_state(r) -> RoomCycleState:
    keys = r.keys() if hasattr(r, "keys") else []

    def _get(name: str):
        return r[name] if name in keys else None

    return RoomCycleState(
        cycle_id=r["cycle_id"],
        room_id=r["room_id"],
        target_temp=r["target_temp"],
        reached_at=_dt(r["reached_at"]),
        vent_closed_at=_dt(r["vent_closed_at"]),
        temp_at_start=_get("temp_at_start"),
        temp_at_end=_get("temp_at_end"),
        trigger_detail=_get("trigger_detail"),
        joined_at=_dt(_get("joined_at")) if _get("joined_at") else None,
        role=_get("role") or "active",
        requested_target=_get("requested_target"),
        effective_target=_get("effective_target"),
        eco_active=bool(_get("eco_active") or 0),
    )


async def get_room_cycle_states(conn: aiosqlite.Connection, cycle_id: str) -> list[RoomCycleState]:
    async with conn.execute("SELECT * FROM room_cycle_states WHERE cycle_id=?", (cycle_id,)) as cur:
        rows = await cur.fetchall()
    return [_row_to_room_cycle_state(r) for r in rows]


async def get_cycle_ids_with_overflow(conn: aiosqlite.Connection, cycle_ids: list[str]) -> set[str]:
    """Return the subset of ``cycle_ids`` that recorded any overflow rooms
    (Issue #254). Used to flag cycles in the Logs list without an N+1 query.
    """
    if not cycle_ids:
        return set()
    placeholders = ",".join("?" for _ in cycle_ids)
    async with conn.execute(
        f"SELECT DISTINCT cycle_id FROM room_cycle_states "
        f"WHERE role='overflow' AND cycle_id IN ({placeholders})",
        tuple(cycle_ids),
    ) as cur:
        rows = await cur.fetchall()
    return {r[0] for r in rows}


async def get_cycle_ids_with_eco(conn: aiosqlite.Connection, cycle_ids: list[str]) -> set[str]:
    """Return the subset of ``cycle_ids`` where Eco Mode relaxed at least one
    room's target (Issue #404). Drives the Logs "Eco Mode" pill without an N+1
    query, mirroring ``get_cycle_ids_with_overflow``.
    """
    if not cycle_ids:
        return set()
    placeholders = ",".join("?" for _ in cycle_ids)
    async with conn.execute(
        f"SELECT DISTINCT cycle_id FROM room_cycle_states "
        f"WHERE eco_active=1 AND cycle_id IN ({placeholders})",
        tuple(cycle_ids),
    ) as cur:
        rows = await cur.fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Cycle diagnostics (Issue #60)
# ---------------------------------------------------------------------------


async def insert_cycle_temp_sample(
    conn: aiosqlite.Connection,
    cycle_id: str,
    room_id: str | None,
    timestamp: datetime,
    room_temp: float | None,
    thermostat_temp: float | None,
    setpoint: float | None,
) -> None:
    await conn.execute(
        """INSERT INTO cycle_temp_samples(cycle_id,room_id,timestamp,room_temp,thermostat_temp,setpoint)
           VALUES(?,?,?,?,?,?)""",
        (
            cycle_id,
            room_id,
            timestamp.replace(tzinfo=None).isoformat(),
            room_temp,
            thermostat_temp,
            setpoint,
        ),
    )
    await conn.commit()


async def get_cycle_temp_samples(
    conn: aiosqlite.Connection,
    cycle_id: str,
    room_id: str | None = None,
) -> list[CycleTempSample]:
    if room_id is None:
        async with conn.execute(
            "SELECT * FROM cycle_temp_samples WHERE cycle_id=? ORDER BY timestamp ASC, id ASC",
            (cycle_id,),
        ) as cur:
            rows = await cur.fetchall()
    else:
        async with conn.execute(
            "SELECT * FROM cycle_temp_samples WHERE cycle_id=? AND room_id=? ORDER BY timestamp ASC, id ASC",
            (cycle_id, room_id),
        ) as cur:
            rows = await cur.fetchall()
    return [
        CycleTempSample(
            id=r["id"],
            cycle_id=r["cycle_id"],
            room_id=r["room_id"],
            timestamp=_dt_required(r["timestamp"]),
            room_temp=r["room_temp"],
            thermostat_temp=r["thermostat_temp"],
            setpoint=r["setpoint"],
        )
        for r in rows
    ]


async def insert_cycle_setpoint_history(
    conn: aiosqlite.Connection,
    cycle_id: str,
    timestamp: datetime,
    setpoint: float,
    reason: str | None = None,
) -> None:
    await conn.execute(
        """INSERT INTO cycle_setpoint_history(cycle_id,timestamp,setpoint,reason)
           VALUES(?,?,?,?)""",
        (cycle_id, timestamp.replace(tzinfo=None).isoformat(), setpoint, reason),
    )
    await conn.commit()


async def get_cycle_setpoint_history(
    conn: aiosqlite.Connection, cycle_id: str
) -> list[CycleSetpointHistory]:
    async with conn.execute(
        "SELECT * FROM cycle_setpoint_history WHERE cycle_id=? ORDER BY timestamp ASC, id ASC",
        (cycle_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [
        CycleSetpointHistory(
            id=r["id"],
            cycle_id=r["cycle_id"],
            timestamp=_dt_required(r["timestamp"]),
            setpoint=r["setpoint"],
            reason=r["reason"],
        )
        for r in rows
    ]


async def insert_cycle_vent_event(
    conn: aiosqlite.Connection,
    cycle_id: str,
    timestamp: datetime,
    entity_id: str,
    room_id: str | None,
    action: str,
    reason: str | None = None,
) -> None:
    await conn.execute(
        """INSERT INTO cycle_vent_events(cycle_id,timestamp,entity_id,room_id,action,reason)
           VALUES(?,?,?,?,?,?)""",
        (cycle_id, timestamp.replace(tzinfo=None).isoformat(), entity_id, room_id, action, reason),
    )
    await conn.commit()


async def get_cycle_vent_events(conn: aiosqlite.Connection, cycle_id: str) -> list[CycleVentEvent]:
    async with conn.execute(
        "SELECT * FROM cycle_vent_events WHERE cycle_id=? ORDER BY timestamp ASC, id ASC",
        (cycle_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [
        CycleVentEvent(
            id=r["id"],
            cycle_id=r["cycle_id"],
            timestamp=_dt_required(r["timestamp"]),
            entity_id=r["entity_id"],
            room_id=r["room_id"],
            action=r["action"],
            reason=r["reason"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Metrics rollup (Issue #85 Phase 1d/1e)
# ---------------------------------------------------------------------------


# ended_reason values written by the cycle engine:
#   "completed"            — cycle hit target (terminate)
#   "aborted: timeout"     — exceeded cycle_timeout_hours
#   "aborted: <other>"     — every other abort path (mode change, system disable, …)
#   NULL                   — pre-Issue #60 rows or in-flight rows; ignored by the rollup
_ROLLUP_REASON_BUCKETS = """
    SUM(CASE WHEN ended_reason = 'completed' THEN 1 ELSE 0 END) AS completed_count,
    SUM(CASE WHEN ended_reason LIKE 'aborted: timeout%' OR ended_reason = 'timeout' THEN 1 ELSE 0 END) AS timeout_count,
    SUM(CASE WHEN ended_reason LIKE 'aborted:%'
              AND NOT (ended_reason LIKE 'aborted: timeout%')
             THEN 1 ELSE 0 END) AS aborted_count
"""


async def rollup_daily_metrics(
    conn: aiosqlite.Connection,
    start_date: str,
    end_date: str,
) -> int:
    """Recompute daily_thermostat_metrics rows for the inclusive local-date range
    [start_date, end_date] (YYYY-MM-DD).

    Buckets each completed cycle by the local date of its `started_at`. Reruns
    are idempotent — every (date, thermostat) row in the range is replaced.
    Returns the number of rows written.
    """
    now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat()
    # Wipe the range first so days that no longer have any cycles drop out
    # (e.g. after a cycle log purge). Cheap because of the composite PK index.
    await conn.execute(
        "DELETE FROM daily_thermostat_metrics WHERE date BETWEEN ? AND ?",
        (start_date, end_date),
    )
    await conn.execute(
        f"""
        INSERT INTO daily_thermostat_metrics (
            date, thermostat_entity_id,
            heating_seconds, cooling_seconds,
            cycle_count, completed_count, timeout_count, aborted_count,
            avg_cycle_duration_seconds,
            avg_outside_temp_at_start, avg_outside_temp_at_end,
            updated_at
        )
        SELECT
            date(started_at, 'localtime') AS day,
            thermostat_entity_id,
            CAST(ROUND(COALESCE(SUM(CASE WHEN mode = 'heating'
                THEN (julianday(ended_at) - julianday(started_at)) * 86400.0 END), 0)) AS INTEGER),
            CAST(ROUND(COALESCE(SUM(CASE WHEN mode = 'cooling'
                THEN (julianday(ended_at) - julianday(started_at)) * 86400.0 END), 0)) AS INTEGER),
            COUNT(*),
            {_ROLLUP_REASON_BUCKETS},
            AVG((julianday(ended_at) - julianday(started_at)) * 86400.0),
            AVG(outside_temp_at_start),
            AVG(outside_temp_at_end),
            ?
        FROM cycle_logs
        WHERE ended_at IS NOT NULL
          AND date(started_at, 'localtime') BETWEEN ? AND ?
        GROUP BY day, thermostat_entity_id
        """,
        (now_iso, start_date, end_date),
    )
    async with conn.execute(
        "SELECT COUNT(*) AS n FROM daily_thermostat_metrics WHERE date BETWEEN ? AND ?",
        (start_date, end_date),
    ) as cur:
        row = await cur.fetchone()
    await conn.commit()
    return int(row["n"]) if row else 0


async def rollup_monthly_metrics(
    conn: aiosqlite.Connection,
    start_month: str,
    end_month: str,
) -> int:
    """Recompute monthly_thermostat_metrics rows for the inclusive month range
    [start_month, end_month] (YYYY-MM).

    Aggregates directly from cycle_logs (not from daily rows) so this is
    self-consistent even if the daily rollup hasn't run for the period yet.
    Idempotent — wipes the range first.
    """
    now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat()
    await conn.execute(
        "DELETE FROM monthly_thermostat_metrics WHERE month BETWEEN ? AND ?",
        (start_month, end_month),
    )
    await conn.execute(
        f"""
        INSERT INTO monthly_thermostat_metrics (
            month, thermostat_entity_id,
            heating_seconds, cooling_seconds,
            cycle_count, completed_count, timeout_count, aborted_count,
            avg_cycle_duration_seconds,
            avg_outside_temp_at_start, avg_outside_temp_at_end,
            updated_at
        )
        SELECT
            strftime('%Y-%m', started_at, 'localtime') AS mon,
            thermostat_entity_id,
            CAST(ROUND(COALESCE(SUM(CASE WHEN mode = 'heating'
                THEN (julianday(ended_at) - julianday(started_at)) * 86400.0 END), 0)) AS INTEGER),
            CAST(ROUND(COALESCE(SUM(CASE WHEN mode = 'cooling'
                THEN (julianday(ended_at) - julianday(started_at)) * 86400.0 END), 0)) AS INTEGER),
            COUNT(*),
            {_ROLLUP_REASON_BUCKETS},
            AVG((julianday(ended_at) - julianday(started_at)) * 86400.0),
            AVG(outside_temp_at_start),
            AVG(outside_temp_at_end),
            ?
        FROM cycle_logs
        WHERE ended_at IS NOT NULL
          AND strftime('%Y-%m', started_at, 'localtime') BETWEEN ? AND ?
        GROUP BY mon, thermostat_entity_id
        """,
        (now_iso, start_month, end_month),
    )
    async with conn.execute(
        "SELECT COUNT(*) AS n FROM monthly_thermostat_metrics WHERE month BETWEEN ? AND ?",
        (start_month, end_month),
    ) as cur:
        row = await cur.fetchone()
    await conn.commit()
    return int(row["n"]) if row else 0


async def get_daily_thermostat_metrics(
    conn: aiosqlite.Connection,
    thermostat_entity_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """Read daily metrics rows. Pass None for any filter to skip it."""
    conditions: list[str] = []
    params: list = []
    if thermostat_entity_id:
        conditions.append("thermostat_entity_id = ?")
        params.append(thermostat_entity_id)
    if start_date:
        conditions.append("date >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("date <= ?")
        params.append(end_date)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    async with conn.execute(
        f"SELECT * FROM daily_thermostat_metrics {where} ORDER BY date ASC, thermostat_entity_id ASC",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_monthly_thermostat_metrics(
    conn: aiosqlite.Connection,
    thermostat_entity_id: str | None = None,
    start_month: str | None = None,
    end_month: str | None = None,
) -> list[dict]:
    """Read monthly metrics rows. Pass None for any filter to skip it."""
    conditions: list[str] = []
    params: list = []
    if thermostat_entity_id:
        conditions.append("thermostat_entity_id = ?")
        params.append(thermostat_entity_id)
    if start_month:
        conditions.append("month >= ?")
        params.append(start_month)
    if end_month:
        conditions.append("month <= ?")
        params.append(end_month)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    async with conn.execute(
        f"SELECT * FROM monthly_thermostat_metrics {where} ORDER BY month ASC, thermostat_entity_id ASC",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Metrics queries (Issue #85 Phase 2) — read straight from cycle_logs so we
# can serve "today" without waiting for the next nightly rollup. The daily
# and monthly aggregate tables are reserved for the long-horizon timeseries
# in Phase 4.
# ---------------------------------------------------------------------------


def cycle_log_range_filter(
    thermostat_id: str | None, start_date: str, end_date: str
) -> tuple[str, list]:
    """Build a WHERE clause + params bucketing cycle_logs by local-date range."""
    where = "ended_at IS NOT NULL AND date(started_at, 'localtime') BETWEEN ? AND ?"
    params: list = [start_date, end_date]
    if thermostat_id is not None:
        where += " AND thermostat_entity_id = ?"
        params.append(thermostat_id)
    return where, params


async def compute_thermostat_summary(
    conn: aiosqlite.Connection,
    thermostat_id: str | None,
    start_date: str,
    end_date: str,
) -> dict:
    """Aggregate heating/cooling hours, cycle counts, completion buckets,
    duty cycle %, source breakdown, and avg outside temp for the local-date
    range [start_date, end_date] (YYYY-MM-DD).

    thermostat_id=None → home aggregate (2b). Specific id → per-thermostat (2a).
    """
    where, params = cycle_log_range_filter(thermostat_id, start_date, end_date)
    sql = f"""
        SELECT
            CAST(ROUND(COALESCE(SUM(CASE WHEN mode='heating'
                THEN (julianday(ended_at) - julianday(started_at)) * 86400.0 END), 0)) AS INTEGER) AS heating_seconds,
            CAST(ROUND(COALESCE(SUM(CASE WHEN mode='cooling'
                THEN (julianday(ended_at) - julianday(started_at)) * 86400.0 END), 0)) AS INTEGER) AS cooling_seconds,
            COUNT(*) AS cycle_count,
            {_ROLLUP_REASON_BUCKETS},
            AVG((julianday(ended_at) - julianday(started_at)) * 86400.0) AS avg_cycle_duration_seconds,
            AVG(outside_temp_at_start) AS avg_outside_temp_at_start,
            AVG(outside_temp_at_end) AS avg_outside_temp_at_end,
            COUNT(DISTINCT thermostat_entity_id) AS thermostat_count
        FROM cycle_logs
        WHERE {where}
    """
    async with conn.execute(sql, params) as cur:
        row = await cur.fetchone()

    heating = int(row["heating_seconds"] or 0) if row else 0
    cooling = int(row["cooling_seconds"] or 0) if row else 0

    # Range duration for duty-cycle calculation: half-open [start 00:00, end+1 00:00) local.
    # We don't know the user's TZ in Python, but for duty cycle purposes the
    # number of seconds between two local midnights N days apart is always
    # 86400 * (days+1) modulo DST; the small DST drift is acceptable for a
    # percentage-style number.
    s = datetime.fromisoformat(start_date)
    e = datetime.fromisoformat(end_date)
    range_seconds = max(1, ((e - s).days + 1) * 86400)
    # For the home aggregate, multiply by thermostat count so the % stays in [0, 100].
    thermo_count = int(row["thermostat_count"] or 0) if row else 0
    if thermostat_id is None and thermo_count > 1:
        range_seconds *= thermo_count
    duty_cycle_pct = ((heating + cooling) / range_seconds) * 100.0

    # Source breakdown: walk rooms_json for each cycle and count each source.
    sql_sources = f"SELECT rooms_json FROM cycle_logs WHERE {where}"
    async with conn.execute(sql_sources, params) as cur:
        rj_rows = await cur.fetchall()
    sources: dict[str, int] = {"schedule": 0, "presence": 0, "override": 0}
    for r in rj_rows:
        try:
            data = json.loads(r["rooms_json"]) if r["rooms_json"] else {}
        except (ValueError, TypeError):
            continue
        seen: set[str] = set()
        for room_meta in data.values():
            src = (room_meta or {}).get("source")
            if src and src not in seen:
                sources[src] = sources.get(src, 0) + 1
                seen.add(src)

    # Eco Mode split (Issue #404): how many cycles / how much runtime happened
    # while Eco Mode was relaxing at least one room's target. Lets trend
    # analysis compare eco-active vs baseline runtime over a date range.
    sql_eco = f"""
        SELECT
            COUNT(*) AS eco_cycle_count,
            CAST(ROUND(COALESCE(SUM(
                (julianday(ended_at) - julianday(started_at)) * 86400.0), 0)) AS INTEGER) AS eco_seconds
        FROM cycle_logs
        WHERE {where}
          AND id IN (SELECT cycle_id FROM room_cycle_states WHERE eco_active=1)
    """
    async with conn.execute(sql_eco, params) as cur:
        eco_row = await cur.fetchone()
    eco_cycle_count = int(eco_row["eco_cycle_count"] or 0) if eco_row else 0
    eco_seconds = int(eco_row["eco_seconds"] or 0) if eco_row else 0

    return {
        "start_date": start_date,
        "end_date": end_date,
        "thermostat_entity_id": thermostat_id,
        "heating_seconds": heating,
        "cooling_seconds": cooling,
        "eco_cycle_count": eco_cycle_count,
        "eco_seconds": eco_seconds,
        "cycle_count": int(row["cycle_count"] or 0) if row else 0,
        "completed_count": int(row["completed_count"] or 0) if row else 0,
        "timeout_count": int(row["timeout_count"] or 0) if row else 0,
        "aborted_count": int(row["aborted_count"] or 0) if row else 0,
        "avg_cycle_duration_seconds": float(row["avg_cycle_duration_seconds"])
        if row and row["avg_cycle_duration_seconds"] is not None
        else None,
        "duty_cycle_pct": round(duty_cycle_pct, 2),
        "avg_outside_temp_at_start": float(row["avg_outside_temp_at_start"])
        if row and row["avg_outside_temp_at_start"] is not None
        else None,
        "avg_outside_temp_at_end": float(row["avg_outside_temp_at_end"])
        if row and row["avg_outside_temp_at_end"] is not None
        else None,
        "thermostat_count": thermo_count,
        "source_breakdown": sources,
    }


async def compute_thermostat_timeseries(
    conn: aiosqlite.Connection,
    thermostat_id: str,
    metric: str,
    granularity: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """One endpoint feeds every chart in Phase 4 — choose the metric +
    granularity to slice. Returns [{period, value, ...}] sorted by period.

    Granularity:
        day   → period = local YYYY-MM-DD
        month → period = local YYYY-MM

    Metric:
        hours          → {heating_seconds, cooling_seconds}
        cycles         → integer cycle count
        avg_duration   → avg cycle duration in seconds
        duty_cycle     → percentage (0–100)
        outside_temp   → avg outside_temp_at_start
        time_to_target → avg seconds from cycle start to first room reaching target (Phase 4f)
        degree_minutes → ∫ |setpoint − thermostat_temp| dt over the bucket (Phase 4k)
        short_cycles   → count of cycles shorter than 10 minutes (Issue #442) —
                         a compressor-health signal (see short-cycle protection, #208)
    """
    if granularity not in ("day", "month"):
        raise ValueError(f"unsupported granularity: {granularity}")
    if metric not in (
        "hours",
        "cycles",
        "avg_duration",
        "duty_cycle",
        "outside_temp",
        "time_to_target",
        "degree_minutes",
        "short_cycles",
    ):
        raise ValueError(f"unsupported metric: {metric}")

    if metric == "time_to_target":
        return await _time_to_target_timeseries(
            conn, thermostat_id, granularity, start_date, end_date
        )
    if metric == "degree_minutes":
        return await _degree_minutes_timeseries(
            conn, thermostat_id, granularity, start_date, end_date
        )

    bucket_expr = (
        "date(started_at, 'localtime')"
        if granularity == "day"
        else "strftime('%Y-%m', started_at, 'localtime')"
    )
    range_filter = (
        bucket_expr + " BETWEEN ? AND ?"
        if granularity == "day"
        else bucket_expr + " BETWEEN ? AND ?"
    )

    sql = f"""
        SELECT
            {bucket_expr} AS period,
            CAST(ROUND(COALESCE(SUM(CASE WHEN mode='heating'
                THEN (julianday(ended_at) - julianday(started_at)) * 86400.0 END), 0)) AS INTEGER) AS heating_seconds,
            CAST(ROUND(COALESCE(SUM(CASE WHEN mode='cooling'
                THEN (julianday(ended_at) - julianday(started_at)) * 86400.0 END), 0)) AS INTEGER) AS cooling_seconds,
            COUNT(*) AS cycle_count,
            AVG((julianday(ended_at) - julianday(started_at)) * 86400.0) AS avg_cycle_duration_seconds,
            AVG(outside_temp_at_start) AS avg_outside_temp_at_start,
            SUM(CASE WHEN (julianday(ended_at) - julianday(started_at)) * 86400.0 < 600
                THEN 1 ELSE 0 END) AS short_cycle_count
        FROM cycle_logs
        WHERE ended_at IS NOT NULL
          AND thermostat_entity_id = ?
          AND {range_filter}
        GROUP BY period
        ORDER BY period ASC
    """
    async with conn.execute(sql, (thermostat_id, start_date, end_date)) as cur:
        rows = await cur.fetchall()

    out: list[dict] = []
    for r in rows:
        period = r["period"]
        if metric == "hours":
            out.append(
                {
                    "period": period,
                    "heating_seconds": int(r["heating_seconds"] or 0),
                    "cooling_seconds": int(r["cooling_seconds"] or 0),
                }
            )
        elif metric == "cycles":
            out.append({"period": period, "value": int(r["cycle_count"] or 0)})
        elif metric == "avg_duration":
            out.append(
                {
                    "period": period,
                    "value": float(r["avg_cycle_duration_seconds"])
                    if r["avg_cycle_duration_seconds"] is not None
                    else None,
                }
            )
        elif metric == "duty_cycle":
            # Per-bucket duty cycle: HVAC seconds / bucket seconds.
            bucket_secs = 86400.0 if granularity == "day" else _seconds_in_month(period)
            total = int(r["heating_seconds"] or 0) + int(r["cooling_seconds"] or 0)
            out.append(
                {
                    "period": period,
                    "value": round((total / bucket_secs) * 100.0, 2) if bucket_secs > 0 else 0.0,
                }
            )
        elif metric == "outside_temp":
            out.append(
                {
                    "period": period,
                    "value": float(r["avg_outside_temp_at_start"])
                    if r["avg_outside_temp_at_start"] is not None
                    else None,
                }
            )
        elif metric == "short_cycles":
            out.append({"period": period, "value": int(r["short_cycle_count"] or 0)})
    return out


def _seconds_in_month(yyyy_mm: str) -> float:
    """Approximate seconds in a YYYY-MM bucket, ignoring DST."""
    year, month = (int(p) for p in yyyy_mm.split("-"))
    next_first = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return (next_first - datetime(year, month, 1)).total_seconds()


async def _time_to_target_timeseries(
    conn: aiosqlite.Connection,
    thermostat_id: str,
    granularity: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Avg seconds from cycle-start to the first room reaching target,
    bucketed by local date/month. (Phase 4f)

    Picks the earliest reached_at across the cycle's room_cycle_states and
    measures from the cycle's started_at (or joined_at if the room joined
    mid-cycle, matching the per-room `compute_room_metrics` semantics).
    """
    bucket_expr = (
        "date(cl.started_at, 'localtime')"
        if granularity == "day"
        else "strftime('%Y-%m', cl.started_at, 'localtime')"
    )
    sql = f"""
        SELECT
            {bucket_expr} AS period,
            AVG(seconds) AS avg_seconds
        FROM (
            SELECT
                cl.id,
                cl.started_at,
                MIN((julianday(rcs.reached_at) - julianday(COALESCE(rcs.joined_at, cl.started_at))) * 86400.0) AS seconds
            FROM cycle_logs cl
            JOIN room_cycle_states rcs ON rcs.cycle_id = cl.id
                AND rcs.role != 'overflow'
            WHERE cl.ended_at IS NOT NULL
              AND cl.thermostat_entity_id = ?
              AND rcs.reached_at IS NOT NULL
              AND {bucket_expr} BETWEEN ? AND ?
            GROUP BY cl.id, cl.started_at
        ) cl
        GROUP BY period
        ORDER BY period ASC
    """
    async with conn.execute(sql, (thermostat_id, start_date, end_date)) as cur:
        rows = await cur.fetchall()
    return [
        {
            "period": r["period"],
            "value": float(r["avg_seconds"]) if r["avg_seconds"] is not None else None,
        }
        for r in rows
    ]


async def _degree_minutes_timeseries(
    conn: aiosqlite.Connection,
    thermostat_id: str,
    granularity: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """∫ |setpoint − thermostat_temp| dt computed from cycle_temp_samples,
    bucketed by local date or month. Walks each cycle's thermostat-level
    trajectory in time order and integrates the absolute delta over each
    inter-sample interval, capped at the cycle's actual end. Returns
    degree-minutes per bucket. (Phase 4k)

    The engine's per-tick sampler writes one row per active room, each
    carrying the same thermostat_temp/setpoint reading (Issue #394) — there
    is no dedicated room_id=NULL writer. Collapse to one row per
    (cycle_id, timestamp) with MAX() (identical across a tick's rooms) so
    a multi-room tick isn't double-counted, and legacy room_id=NULL rows
    still work unchanged.

    Each cycle's final interval — its last sample up to `ended_at` — is
    flushed after walking that cycle's rows, so a cycle with only one
    sample (typical of a short cycle) still contributes rather than being
    silently dropped.
    """
    sql = """
        SELECT cl.id AS cycle_id,
               cl.started_at,
               cl.ended_at,
               s.timestamp,
               MAX(s.thermostat_temp) AS thermostat_temp,
               MAX(s.setpoint) AS setpoint
        FROM cycle_logs cl
        JOIN cycle_temp_samples s ON s.cycle_id = cl.id
        WHERE cl.ended_at IS NOT NULL
          AND cl.thermostat_entity_id = ?
          AND s.thermostat_temp IS NOT NULL
          AND s.setpoint IS NOT NULL
          AND date(cl.started_at, 'localtime') BETWEEN ? AND ?
        GROUP BY cl.id, cl.started_at, cl.ended_at, s.timestamp
        ORDER BY cl.id, s.timestamp ASC
    """
    async with conn.execute(sql, (thermostat_id, start_date, end_date)) as cur:
        rows = await cur.fetchall()

    # Walk each cycle's samples in order, accumulating into a (period -> minutes)
    # dict. Every cycle's tail — its last sample up to the cycle's own ended_at —
    # is flushed after the inner loop, so a cycle with only one sample (or any
    # cycle's final inter-sample gap) still contributes instead of silently
    # dropping out.
    by_period: dict[str, float] = {}
    for _cycle_id, group in groupby(rows, key=lambda r: r["cycle_id"]):
        cycle_rows = list(group)
        cur_end = datetime.fromisoformat(cycle_rows[0]["ended_at"]).replace(tzinfo=UTC)
        last_ts: datetime | None = None
        last_delta: float | None = None
        for r in cycle_rows:
            ts = datetime.fromisoformat(r["timestamp"]).replace(tzinfo=UTC)
            delta = abs(float(r["setpoint"]) - float(r["thermostat_temp"]))
            if last_ts is not None and last_delta is not None:
                interval_end = min(ts, cur_end)
                dt_min = max(0.0, (interval_end - last_ts).total_seconds() / 60.0)
                period_key = _period_key(tz.to_local(last_ts), granularity)
                by_period[period_key] = by_period.get(period_key, 0.0) + last_delta * dt_min
            last_ts = ts
            last_delta = delta
        if last_ts is not None and last_delta is not None and cur_end > last_ts:
            dt_min = (cur_end - last_ts).total_seconds() / 60.0
            period_key = _period_key(tz.to_local(last_ts), granularity)
            by_period[period_key] = by_period.get(period_key, 0.0) + last_delta * dt_min

    return [
        {"period": p, "value": round(v, 2)}
        for p, v in sorted(by_period.items())
        if start_date <= p <= end_date or granularity == "month"
    ]


def _period_key(local_dt: datetime, granularity: str) -> str:
    if granularity == "day":
        return local_dt.strftime("%Y-%m-%d")
    return local_dt.strftime("%Y-%m")


async def compute_overshoot_histogram(
    conn: aiosqlite.Connection,
    thermostat_id: str,
    start_date: str,
    end_date: str,
    bin_size: float = 1.0,
    max_bins: int = 6,
) -> dict:
    """Histogram of how often + by how much the engine overshot per-room
    targets in completed cycles. (Phase 4l)

    Overshoot for each room participation = the worst observed temperature
    on the wrong side of the target during the cycle:
        cooling: max( target − min(thermostat_temp_seen), 0 )
        heating: max( max(thermostat_temp_seen) − target, 0 )

    Uses cycle_temp_samples (room-level when available, falling back to
    thermostat-level) joined to room_cycle_states for the target. Bins
    are [0, bin_size), [bin_size, 2·bin_size), …, with the last bucket
    being an open-ended "≥(max_bins-1)·bin_size" so the chart always has
    a stable shape regardless of outliers.
    """
    where, params = cycle_log_range_filter(thermostat_id, start_date, end_date)
    sql = f"""
        SELECT cl.id AS cycle_id, cl.mode,
               rcs.room_id, rcs.target_temp,
               s.room_id AS sample_room_id,
               s.room_temp, s.thermostat_temp
        FROM cycle_logs cl
        JOIN room_cycle_states rcs ON rcs.cycle_id = cl.id
            AND rcs.role != 'overflow'
        JOIN cycle_temp_samples s ON s.cycle_id = cl.id
            AND (s.room_id = rcs.room_id OR s.room_id IS NULL)
        WHERE {where}
    """
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()

    # Walk samples, tracking per-(cycle_id, room_id) extremes. Room-level samples
    # (the join's `s.room_id = rcs.room_id` arm) and thermostat-level samples
    # (the `s.room_id IS NULL` arm) are tracked separately so the thermostat-level
    # fallback is applied per (cycle, room): a room's overshoot is computed from
    # its own samples whenever it has any, and only falls back to thermostat-level
    # samples when it produced no room-level temperature (Issue #290).
    extremes: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["cycle_id"], r["room_id"])
        ext = extremes.setdefault(
            key,
            {
                "mode": r["mode"],
                "target": float(r["target_temp"]),
                "room_min": None,
                "room_max": None,
                "thermo_min": None,
                "thermo_max": None,
            },
        )
        if r["sample_room_id"] is not None:
            # Room-level sample: prefer the room temperature, falling back to the
            # thermostat reading captured in the same row when the room sensor was
            # unavailable at that tick.
            temp = r["room_temp"] if r["room_temp"] is not None else r["thermostat_temp"]
            lo_key, hi_key = "room_min", "room_max"
        else:
            # Thermostat-level sample (room_id IS NULL): fallback only.
            temp = r["thermostat_temp"]
            lo_key, hi_key = "thermo_min", "thermo_max"
        if temp is None:
            continue
        t = float(temp)
        if ext[lo_key] is None or t < ext[lo_key]:
            ext[lo_key] = t
        if ext[hi_key] is None or t > ext[hi_key]:
            ext[hi_key] = t

    bins = [0] * max_bins
    overshoots: list[float] = []
    total_room_cycles = len(extremes)
    overshot = 0
    for ext in extremes.values():
        target = ext["target"]
        # Prefer room-level extremes; fall back to thermostat-level samples only
        # for rooms that produced no room-level temperature.
        if ext["room_min"] is not None or ext["room_max"] is not None:
            min_temp, max_temp = ext["room_min"], ext["room_max"]
        else:
            min_temp, max_temp = ext["thermo_min"], ext["thermo_max"]
        if ext["mode"] == "cooling" and min_temp is not None:
            os = max(target - min_temp, 0.0)
        elif ext["mode"] == "heating" and max_temp is not None:
            os = max(max_temp - target, 0.0)
        else:
            continue
        overshoots.append(os)
        if os > 0:
            overshot += 1
        idx = min(int(os // bin_size), max_bins - 1)
        bins[idx] += 1

    labels = []
    for i in range(max_bins):
        lo = i * bin_size
        if i == max_bins - 1:
            labels.append(f"≥{lo:g}°F")
        else:
            labels.append(f"{lo:g}–{lo + bin_size:g}°F")

    return {
        "thermostat_entity_id": thermostat_id,
        "start_date": start_date,
        "end_date": end_date,
        "bin_size": bin_size,
        "labels": labels,
        "counts": bins,
        "total_room_cycles": total_room_cycles,
        "overshot_count": overshot,
        "overshot_pct": round((overshot / total_room_cycles) * 100.0, 2)
        if total_room_cycles
        else 0.0,
        "max_overshoot_f": round(max(overshoots), 2) if overshoots else 0.0,
        "avg_overshoot_f": round(sum(overshoots) / len(overshoots), 2) if overshoots else 0.0,
    }


async def compute_room_metrics(
    conn: aiosqlite.Connection,
    thermostat_id: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Per-room participation rate, heating vs cooling time, and average
    time-to-target for cycles in the range. (Issue #85 Phase 2d)"""
    where, params = cycle_log_range_filter(thermostat_id, start_date, end_date)
    # Total cycle count for this thermostat in the range — denominator for participation.
    async with conn.execute(f"SELECT COUNT(*) AS n FROM cycle_logs WHERE {where}", params) as cur:
        _row = await cur.fetchone()
        total_cycles = int((_row["n"] if _row is not None else 0) or 0)

    sql = """
        SELECT
            r.id AS room_id,
            r.name AS room_name,
            COUNT(CASE WHEN cl.id IS NOT NULL THEN rcs.cycle_id END) AS participation_count,
            CAST(ROUND(COALESCE(SUM(CASE WHEN cl.mode='heating'
                THEN (julianday(COALESCE(rcs.vent_closed_at, cl.ended_at)) - julianday(COALESCE(rcs.joined_at, cl.started_at))) * 86400.0 END), 0)) AS INTEGER) AS heating_seconds,
            CAST(ROUND(COALESCE(SUM(CASE WHEN cl.mode='cooling'
                THEN (julianday(COALESCE(rcs.vent_closed_at, cl.ended_at)) - julianday(COALESCE(rcs.joined_at, cl.started_at))) * 86400.0 END), 0)) AS INTEGER) AS cooling_seconds,
            AVG(CASE WHEN cl.id IS NOT NULL AND rcs.reached_at IS NOT NULL
                THEN (julianday(rcs.reached_at) - julianday(COALESCE(rcs.joined_at, cl.started_at))) * 86400.0 END) AS avg_time_to_target_seconds
        FROM rooms r
        LEFT JOIN room_cycle_states rcs ON rcs.room_id = r.id
            AND rcs.role != 'overflow'
        LEFT JOIN cycle_logs cl ON cl.id = rcs.cycle_id
            AND cl.ended_at IS NOT NULL
            AND date(cl.started_at, 'localtime') BETWEEN ? AND ?
            AND cl.thermostat_entity_id = ?
        WHERE r.thermostat_entity_id = ?
        GROUP BY r.id, r.name
        ORDER BY r.name ASC
    """
    async with conn.execute(sql, (start_date, end_date, thermostat_id, thermostat_id)) as cur:
        rows = await cur.fetchall()

    out = []
    for r in rows:
        participation = int(r["participation_count"] or 0)
        out.append(
            {
                "room_id": r["room_id"],
                "room_name": r["room_name"],
                "participation_count": participation,
                "participation_rate": round(participation / total_cycles, 4)
                if total_cycles > 0
                else 0.0,
                "heating_seconds": int(r["heating_seconds"] or 0),
                "cooling_seconds": int(r["cooling_seconds"] or 0),
                "avg_time_to_target_seconds": float(r["avg_time_to_target_seconds"])
                if r["avg_time_to_target_seconds"] is not None
                else None,
            }
        )
    return out


async def compute_cycles_vs_outside_temp(
    conn: aiosqlite.Connection,
    thermostat_id: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Every completed cycle as a scatter point: outside temp at start vs cycle
    duration in minutes, with mode for series colouring. (Phase 2e)"""
    where, params = cycle_log_range_filter(thermostat_id, start_date, end_date)
    # `eco_active` (Issue #404) flags cycles Eco Mode relaxed, so a scatter can
    # colour or filter eco vs baseline cycles against outdoor temperature.
    sql = f"""
        SELECT
            id,
            mode,
            outside_temp_at_start,
            outside_temp_at_end,
            (julianday(ended_at) - julianday(started_at)) * 1440.0 AS duration_minutes,
            started_at,
            EXISTS(
                SELECT 1 FROM room_cycle_states rcs
                WHERE rcs.cycle_id = cycle_logs.id AND rcs.eco_active=1
            ) AS eco_active
        FROM cycle_logs
        WHERE {where} AND outside_temp_at_start IS NOT NULL
        ORDER BY started_at ASC
    """
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [
        {
            "cycle_id": r["id"],
            "mode": r["mode"],
            "outside_temp": float(r["outside_temp_at_start"]),
            "outside_temp_at_end": float(r["outside_temp_at_end"])
            if r["outside_temp_at_end"] is not None
            else None,
            "duration_minutes": float(r["duration_minutes"]),
            "started_at": r["started_at"],
            "eco_active": bool(r["eco_active"]),
        }
        for r in rows
    ]


async def compute_eco_impact(
    conn: aiosqlite.Connection,
    thermostat_id: str | None,
    start_date: str,
    end_date: str,
) -> dict:
    """Eco Mode impact over a local-date range (Issue #404).

    Splits cycles/runtime by whether Eco Mode relaxed a target, reports the
    average drift actually applied (°F, ``effective − requested`` over the
    eco-active room-cycles), a per-day series of the same split (Issue #442,
    for the Metrics-page trend charts), and a per-room breakdown.
    ``thermostat_id=None`` aggregates across the whole home. All temperatures
    are °F.

    This exposes the raw split so savings can be *inferred* later from runtime
    reduction (Plenum can't read kWh) — it deliberately does not claim a kWh
    number. Depends on the date-range params shared with the other metrics.
    """
    where, params = cycle_log_range_filter(thermostat_id, start_date, end_date)

    # Cycle-level split: total vs eco-active cycles and their runtime seconds.
    sql_cycles = f"""
        SELECT
            COUNT(*) AS total_cycles,
            CAST(ROUND(COALESCE(SUM(
                (julianday(ended_at) - julianday(started_at)) * 86400.0), 0)) AS INTEGER) AS total_seconds,
            COALESCE(SUM(CASE WHEN eco THEN 1 ELSE 0 END), 0) AS eco_active_cycles,
            CAST(ROUND(COALESCE(SUM(CASE WHEN eco
                THEN (julianday(ended_at) - julianday(started_at)) * 86400.0 END), 0)) AS INTEGER) AS eco_active_seconds
        FROM (
            SELECT
                started_at, ended_at,
                EXISTS(
                    SELECT 1 FROM room_cycle_states rcs
                    WHERE rcs.cycle_id = cycle_logs.id AND rcs.eco_active=1
                ) AS eco
            FROM cycle_logs
            WHERE {where}
        )
    """
    async with conn.execute(sql_cycles, params) as cur:
        crow = await cur.fetchone()

    # Room-cycle drift: average °F relaxation applied, and per-room breakdown.
    # Join room_cycle_states to their cycle so the same date/thermostat filter
    # applies. `where` references bare cycle_logs columns, so alias as needed.
    where_cl = where.replace("thermostat_entity_id", "cycle_logs.thermostat_entity_id")
    sql_rooms = f"""
        SELECT
            rcs.room_id AS room_id,
            COUNT(*) AS eco_active_cycles,
            -- Drift MAGNITUDE (°F): cooling relaxes warmer (+) and heating cooler
            -- (−), so use ABS so "average drift applied" is a positive number
            -- rather than cancelling across modes.
            AVG(ABS(rcs.effective_target - rcs.requested_target)) AS avg_drift,
            MAX(ABS(rcs.effective_target - rcs.requested_target)) AS max_drift
        FROM room_cycle_states rcs
        JOIN cycle_logs ON cycle_logs.id = rcs.cycle_id
        LEFT JOIN rooms ON rooms.id = rcs.room_id
        WHERE {where_cl}
          AND rcs.eco_active=1
          AND rcs.requested_target IS NOT NULL
          AND rcs.effective_target IS NOT NULL
        GROUP BY rcs.room_id
        -- Tie-break by room name, then id: bare DESC on the count leaves tied
        -- rooms in random-UUID order, which flipped the "Eco drift by room"
        -- chart's rows between fresh E2E stacks and churned the metrics
        -- goldens (Issue #442).
        ORDER BY eco_active_cycles DESC, rooms.name ASC, rcs.room_id ASC
    """
    async with conn.execute(sql_rooms, params) as cur:
        room_rows = await cur.fetchall()

    # Per-day split (Issue #442): the same eco-vs-total cycle/runtime numbers
    # bucketed by local date, so the Metrics page can chart engagement and
    # drift as a trend rather than a single range-wide total.
    sql_days = f"""
        SELECT
            day,
            COUNT(*) AS total_cycles,
            CAST(ROUND(COALESCE(SUM(secs), 0)) AS INTEGER) AS total_seconds,
            COALESCE(SUM(CASE WHEN eco THEN 1 ELSE 0 END), 0) AS eco_active_cycles,
            CAST(ROUND(COALESCE(SUM(CASE WHEN eco THEN secs END), 0)) AS INTEGER)
                AS eco_active_seconds
        FROM (
            SELECT
                date(started_at, 'localtime') AS day,
                (julianday(ended_at) - julianday(started_at)) * 86400.0 AS secs,
                EXISTS(
                    SELECT 1 FROM room_cycle_states rcs
                    WHERE rcs.cycle_id = cycle_logs.id AND rcs.eco_active=1
                ) AS eco
            FROM cycle_logs
            WHERE {where}
        )
        GROUP BY day
        ORDER BY day ASC
    """
    async with conn.execute(sql_days, params) as cur:
        day_rows = await cur.fetchall()

    # Average drift actually applied per day (°F, magnitude — same ABS
    # rationale as the per-room query above), over eco-active room-cycles.
    sql_day_drift = f"""
        SELECT
            date(cycle_logs.started_at, 'localtime') AS day,
            AVG(ABS(rcs.effective_target - rcs.requested_target)) AS avg_drift
        FROM room_cycle_states rcs
        JOIN cycle_logs ON cycle_logs.id = rcs.cycle_id
        WHERE {where_cl}
          AND rcs.eco_active=1
          AND rcs.requested_target IS NOT NULL
          AND rcs.effective_target IS NOT NULL
        GROUP BY day
    """
    async with conn.execute(sql_day_drift, params) as cur:
        drift_by_day = {r["day"]: float(r["avg_drift"] or 0.0) for r in await cur.fetchall()}

    day_series = [
        {
            "date": r["day"],
            "total_cycles": int(r["total_cycles"] or 0),
            "total_seconds": int(r["total_seconds"] or 0),
            "eco_active_cycles": int(r["eco_active_cycles"] or 0),
            "eco_active_seconds": int(r["eco_active_seconds"] or 0),
            "avg_drift_f": round(drift_by_day.get(r["day"], 0.0), 2),
        }
        for r in day_rows
    ]

    rooms = []
    drift_weighted_sum = 0.0
    drift_weight = 0
    for r in room_rows:
        room = await get_room(conn, r["room_id"])
        avg_drift = float(r["avg_drift"]) if r["avg_drift"] is not None else 0.0
        cycles = int(r["eco_active_cycles"] or 0)
        drift_weighted_sum += avg_drift * cycles
        drift_weight += cycles
        rooms.append(
            {
                "room_id": r["room_id"],
                "name": room.name if room else None,
                "eco_active_cycles": cycles,
                "avg_drift_f": round(avg_drift, 2),
                "max_drift_f": round(float(r["max_drift"]), 2)
                if r["max_drift"] is not None
                else 0.0,
            }
        )

    return {
        "start_date": start_date,
        "end_date": end_date,
        "thermostat_entity_id": thermostat_id,
        "total_cycles": int(crow["total_cycles"] or 0) if crow else 0,
        "total_seconds": int(crow["total_seconds"] or 0) if crow else 0,
        "eco_active_cycles": int(crow["eco_active_cycles"] or 0) if crow else 0,
        "eco_active_seconds": int(crow["eco_active_seconds"] or 0) if crow else 0,
        "avg_drift_f": round(drift_weighted_sum / drift_weight, 2) if drift_weight else 0.0,
        "days": day_series,
        "rooms": rooms,
    }


async def compute_hour_heatmap(
    conn: aiosqlite.Connection,
    thermostat_id: str,
    start_date: str,
    end_date: str,
) -> dict:
    """7×24 grid (Mon..Sun × hours 0..23) of HVAC seconds within each cell,
    summed over the range. Each cycle contributes seconds to every hour-cell
    it overlaps. SQLite's strftime gives weekday 0=Sun..6=Sat — we shift to
    0=Mon..6=Sun for the chart's expected ordering. (Phase 2f)"""
    where, params = cycle_log_range_filter(thermostat_id, start_date, end_date)
    sql = f"""
        SELECT id, started_at, ended_at, mode
        FROM cycle_logs
        WHERE {where}
    """
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()

    grid = [[0 for _ in range(24)] for _ in range(7)]  # grid[dow][hour]
    for r in rows:
        # Parse as naive UTC, then convert to the configured local zone for
        # bucketing via the centralized tz helper.
        s = tz.to_local(datetime.fromisoformat(r["started_at"]).replace(tzinfo=UTC))
        e = tz.to_local(datetime.fromisoformat(r["ended_at"]).replace(tzinfo=UTC))
        cur_t = s.replace(minute=0, second=0, microsecond=0)
        while cur_t < e:
            slot_end = cur_t + timedelta(hours=1)
            overlap = min(e, slot_end) - max(s, cur_t)
            secs = max(0, int(overlap.total_seconds()))
            if secs:
                # Python weekday: Monday=0..Sunday=6 (matches our wanted layout).
                grid[cur_t.weekday()][cur_t.hour] += secs
            cur_t = slot_end
    return {
        "start_date": start_date,
        "end_date": end_date,
        "thermostat_entity_id": thermostat_id,
        # Day-of-week labels keyed to the row order (Mon..Sun).
        "day_labels": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "grid_seconds": grid,
    }


async def get_vent_events_in_range(
    conn: aiosqlite.Connection,
    thermostat_id: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Cycle-boundary vent events within the range, joined with their
    parent cycle so the chart can colour by mode. (Phase 2g — note: cycle
    boundary only; the UI must show a disclosure for that limitation.)"""
    sql = """
        SELECT
            v.cycle_id,
            v.timestamp,
            v.entity_id,
            v.room_id,
            v.action,
            v.reason,
            cl.mode AS cycle_mode,
            cl.started_at AS cycle_started_at,
            cl.ended_at AS cycle_ended_at
        FROM cycle_vent_events v
        JOIN cycle_logs cl ON cl.id = v.cycle_id
        WHERE cl.thermostat_entity_id = ?
          AND date(cl.started_at, 'localtime') BETWEEN ? AND ?
        ORDER BY v.timestamp ASC, v.id ASC
    """
    async with conn.execute(sql, (thermostat_id, start_date, end_date)) as cur:
        rows = await cur.fetchall()
    return [
        {
            "cycle_id": r["cycle_id"],
            "timestamp": r["timestamp"],
            "entity_id": r["entity_id"],
            "room_id": r["room_id"],
            "action": r["action"],
            "reason": r["reason"],
            "cycle_mode": r["cycle_mode"],
            "cycle_started_at": r["cycle_started_at"],
            "cycle_ended_at": r["cycle_ended_at"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# System settings
# ---------------------------------------------------------------------------


async def get_system_setting(conn: aiosqlite.Connection, key: str, default: str = "") -> str:
    async with conn.execute("SELECT value FROM system_settings WHERE key=?", (key,)) as cur:
        row = await cur.fetchone()
    return row["value"] if row else default


async def set_system_setting(conn: aiosqlite.Connection, key: str, value: str) -> None:
    await conn.execute(
        """INSERT INTO system_settings(key,value) VALUES(?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (key, value),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# MCP bearer tokens (Issue #373)
# ---------------------------------------------------------------------------


async def create_mcp_token(
    conn: aiosqlite.Connection,
    *,
    token_id: str,
    label: str,
    token_hash: str,
    scope: str,
    created_at: str,
) -> None:
    """Persist a minted MCP token. Only the hash is stored — never the secret."""
    await conn.execute(
        """INSERT INTO mcp_tokens(id, label, token_hash, scope, created_at, last_used_at)
           VALUES(?,?,?,?,?,NULL)""",
        (token_id, label, token_hash, scope, created_at),
    )
    await conn.commit()


async def list_mcp_tokens(conn: aiosqlite.Connection) -> list[dict]:
    """All tokens' metadata (id, label, scope, timestamps) — NEVER the hash."""
    async with conn.execute(
        """SELECT id, label, scope, created_at, last_used_at
           FROM mcp_tokens ORDER BY created_at DESC"""
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_mcp_token_by_hash(conn: aiosqlite.Connection, token_hash: str) -> dict | None:
    """Look up a token by its SHA-256 hash (the presentation path). None if absent."""
    async with conn.execute(
        "SELECT id, label, scope FROM mcp_tokens WHERE token_hash=?", (token_hash,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def touch_mcp_token(conn: aiosqlite.Connection, token_hash: str, when: str) -> None:
    """Record that a token was just used (for the UI's 'last used' column)."""
    await conn.execute(
        "UPDATE mcp_tokens SET last_used_at=? WHERE token_hash=?", (when, token_hash)
    )
    await conn.commit()


async def delete_mcp_token(conn: aiosqlite.Connection, token_id: str) -> bool:
    """Revoke a token by id. Returns True if a row was deleted."""
    cur = await conn.execute("DELETE FROM mcp_tokens WHERE id=?", (token_id,))
    await conn.commit()
    return cur.rowcount > 0


async def get_mcp_token(conn: aiosqlite.Connection, token_id: str) -> dict | None:
    """A single token's metadata by id (id, label, scope, timestamps) — NEVER the hash."""
    async with conn.execute(
        "SELECT id, label, scope, created_at, last_used_at FROM mcp_tokens WHERE id=?",
        (token_id,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def update_mcp_token_scope(conn: aiosqlite.Connection, token_id: str, scope: str) -> bool:
    """Change a token's scope in place. Returns True if a row was updated."""
    cur = await conn.execute("UPDATE mcp_tokens SET scope=? WHERE id=?", (scope, token_id))
    await conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------

_EVENT_LOG_MAX = 5000
_TRIM_EVERY = 100  # trim only every N inserts to avoid per-insert scans


async def insert_event_log(
    conn: aiosqlite.Connection,
    timestamp: str,
    level: str,
    category: str,
    message: str,
    details: str | None,
) -> int:
    """Insert an event log row and return its rowid. Trims to _EVENT_LOG_MAX periodically."""
    async with conn.execute(
        "INSERT INTO event_log(timestamp,level,category,message,details) VALUES(?,?,?,?,?)",
        (timestamp, level, category, message, details),
    ) as cur:
        rowid = cur.lastrowid
    await conn.commit()
    # Periodic trim — avoid subquery overhead on every insert. Demo rows
    # (details JSON carries "demo": true — the Live Feed counterpart of the
    # demo- cycle-id prefix, Issue #442) are exempt: they are seeded once with
    # the oldest ids/timestamps and would otherwise be the first rows trimmed.
    if rowid and rowid % _TRIM_EVERY == 0:
        await conn.execute(
            """DELETE FROM event_log WHERE id < (
                SELECT MIN(id) FROM (
                    SELECT id FROM event_log ORDER BY id DESC LIMIT ?
                )
            ) AND COALESCE(json_extract(details, '$.demo'), 0) != 1""",
            (_EVENT_LOG_MAX,),
        )
        await conn.commit()
    return rowid or 0


async def get_event_logs(
    conn: aiosqlite.Connection,
    limit: int = 200,
    offset: int = 0,
    category: str | None = None,
    since: str | None = None,
    until: str | None = None,
    levels: list[str] | None = None,
) -> list[dict]:
    conditions: list[str] = []
    params: list = []
    if category:
        conditions.append("category=?")
        params.append(category)
    if since:
        conditions.append("timestamp >= ?")
        params.append(since)
    if until:
        conditions.append("timestamp <= ?")
        params.append(until)
    if levels:
        placeholders = ",".join("?" * len(levels))
        conditions.append(f"level IN ({placeholders})")
        params.extend(levels)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params += [limit, offset]
    async with conn.execute(
        f"SELECT * FROM event_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [
        {
            "id": r["id"],
            "timestamp": r["timestamp"],
            "level": r["level"],
            "category": r["category"],
            "message": r["message"],
            "details": json.loads(r["details"]) if r["details"] else None,
        }
        for r in rows
    ]


async def purge_event_logs(conn: aiosqlite.Connection, older_than_days: int) -> int:
    """Delete event logs older than N days. Returns number of rows deleted.

    Demo rows (details JSON carries ``"demo": true`` — the Live Feed
    counterpart of the ``demo-`` cycle-id prefix, Issue #442) are exempt: they
    live in a fixed past window that retention would otherwise delete on the
    next purge pass, and they are wiped/rewritten wholesale by
    ``demo_seed.seed_demo_metrics`` instead.
    """
    cutoff = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=older_than_days)).isoformat()
    async with conn.execute(
        "DELETE FROM event_log WHERE timestamp < ? "
        "AND COALESCE(json_extract(details, '$.demo'), 0) != 1",
        (cutoff,),
    ) as cur:
        count = cur.rowcount or 0
    await conn.commit()
    return count


async def clear_event_logs(conn: aiosqlite.Connection) -> None:
    """Delete all event log rows (manual user-initiated clear)."""
    await conn.execute("DELETE FROM event_log")
    await conn.commit()
