"""
SQLite database layer using aiosqlite.
All public functions are async and accept an aiosqlite.Connection.
Call `init_db(conn)` once at startup to create tables.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, time, timedelta

import aiosqlite

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
    temp_offset REAL NOT NULL DEFAULT 0.0
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
    target_temp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS thermostat_configs (
    thermostat_entity_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    default_temp REAL,
    min_setpoint REAL NOT NULL DEFAULT 60.0,
    max_setpoint REAL NOT NULL DEFAULT 85.0,
    deadband REAL NOT NULL DEFAULT 0.5,
    max_vent_closed_min INTEGER NOT NULL DEFAULT 0,
    min_open_vents INTEGER NOT NULL DEFAULT 1,
    overshoot_delta REAL NOT NULL DEFAULT 2.0,
    cycle_timeout_hours REAL NOT NULL DEFAULT 3.0
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
    outside_temp_at_end REAL
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
"""


async def init_db(conn: aiosqlite.Connection) -> None:
    await conn.executescript(SCHEMA)
    # Migrations for columns added after initial schema
    for migration in _MIGRATIONS:
        try:
            await conn.execute(migration)
            await conn.commit()
        except Exception:
            pass  # column already exists
    # Data migration: fix holdover timestamps stored in local time (Issue #65)
    await _migrate_holdover_timestamps_to_utc(conn)
    log.info("Database initialised")


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
            rows = await cur.fetchall()

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


_MIGRATIONS = [
    "ALTER TABLE rooms ADD COLUMN temp_offset REAL NOT NULL DEFAULT 0.0",
    "ALTER TABLE thermostat_configs ADD COLUMN name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE thermostat_configs ADD COLUMN default_temp REAL",
    "ALTER TABLE thermostat_configs ADD COLUMN reconciliation_interval_min INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE room_vents ADD COLUMN control_method TEXT NOT NULL DEFAULT 'open_close'",
    # Cycle diagnostics (Issue #60)
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
    # Outside-temperature capture (Issue #85 Phase 1c)
    "ALTER TABLE cycle_logs ADD COLUMN outside_temp_at_start REAL",
    "ALTER TABLE cycle_logs ADD COLUMN outside_temp_at_end REAL",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dt(s: str | None) -> datetime | None:
    """Read a datetime string from the DB as UTC-aware."""
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


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
    )


async def upsert_room(conn: aiosqlite.Connection, room: Room) -> None:
    await conn.execute(
        """INSERT INTO rooms (id,name,thermostat_entity_id,include_thermostat_sensor,
           system_wide_temp,presence_holdover_hours,notes,temp_offset)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name,
             thermostat_entity_id=excluded.thermostat_entity_id,
             include_thermostat_sensor=excluded.include_thermostat_sensor,
             system_wide_temp=excluded.system_wide_temp,
             presence_holdover_hours=excluded.presence_holdover_hours,
             notes=excluded.notes,
             temp_offset=excluded.temp_offset
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


def _row_to_schedule(row) -> Schedule:
    return Schedule(
        id=row["id"],
        room_id=row["room_id"],
        days_of_week=json.loads(row["days_of_week"]),
        start_time=_t(row["start_time"]),
        end_time=_t(row["end_time"]),
        target_temp=row["target_temp"],
    )


async def upsert_schedule(conn: aiosqlite.Connection, s: Schedule) -> None:
    await conn.execute(
        """INSERT INTO schedules(id,room_id,days_of_week,start_time,end_time,target_temp)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             days_of_week=excluded.days_of_week,
             start_time=excluded.start_time,
             end_time=excluded.end_time,
             target_temp=excluded.target_temp
        """,
        (
            s.id,
            s.room_id,
            json.dumps(s.days_of_week),
            s.start_time.isoformat(),
            s.end_time.isoformat(),
            s.target_temp,
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
    return ThermostatConfig(
        thermostat_entity_id=row["thermostat_entity_id"],
        name=row["name"] if row["name"] is not None else "",
        default_temp=row["default_temp"],
        min_setpoint=row["min_setpoint"],
        max_setpoint=row["max_setpoint"],
        deadband=row["deadband"],
        max_vent_closed_min=row["max_vent_closed_min"],
        min_open_vents=row["min_open_vents"],
        overshoot_delta=row["overshoot_delta"],
        cycle_timeout_hours=row["cycle_timeout_hours"],
        reconciliation_interval_min=int(row["reconciliation_interval_min"] or 0),
    )


async def upsert_thermostat_config(conn: aiosqlite.Connection, tc: ThermostatConfig) -> None:
    await conn.execute(
        """INSERT INTO thermostat_configs
           (thermostat_entity_id,name,default_temp,min_setpoint,max_setpoint,deadband,
            max_vent_closed_min,min_open_vents,overshoot_delta,cycle_timeout_hours,
            reconciliation_interval_min)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(thermostat_entity_id) DO UPDATE SET
             name=excluded.name,
             default_temp=excluded.default_temp,
             min_setpoint=excluded.min_setpoint,
             max_setpoint=excluded.max_setpoint,
             deadband=excluded.deadband,
             max_vent_closed_min=excluded.max_vent_closed_min,
             min_open_vents=excluded.min_open_vents,
             overshoot_delta=excluded.overshoot_delta,
             cycle_timeout_hours=excluded.cycle_timeout_hours,
             reconciliation_interval_min=excluded.reconciliation_interval_min
        """,
        (
            tc.thermostat_entity_id,
            tc.name,
            tc.default_temp,
            tc.min_setpoint,
            tc.max_setpoint,
            tc.deadband,
            tc.max_vent_closed_min,
            tc.min_open_vents,
            tc.overshoot_delta,
            tc.cycle_timeout_hours,
            tc.reconciliation_interval_min,
        ),
    )
    await conn.commit()


async def delete_thermostat_config(conn: aiosqlite.Connection, entity_id: str) -> None:
    await conn.execute("DELETE FROM thermostat_configs WHERE thermostat_entity_id=?", (entity_id,))
    await conn.commit()


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
        expires_at=_dt(row["expires_at"]),
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
            last_detected_at=_dt(r["last_detected_at"]),
            expires_at=_dt(r["expires_at"]),
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
        last_detected_at=_dt(row["last_detected_at"]),
        expires_at=_dt(row["expires_at"]),
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
        f"  WHERE cl.ended_at IS NULL AND rcs.room_id IN ({placeholders})"
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
        started_at=_dt(r["started_at"]),
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
    )


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
    """Delete cycle logs older than N days. Returns number of rows deleted."""
    cutoff = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=older_than_days)).isoformat()
    async with conn.execute("DELETE FROM cycle_logs WHERE started_at < ?", (cutoff,)) as cur:
        count = cur.rowcount or 0
    await conn.commit()
    return count


async def upsert_room_cycle_state(conn: aiosqlite.Connection, rcs: RoomCycleState) -> None:
    await conn.execute(
        """INSERT INTO room_cycle_states(
            cycle_id, room_id, target_temp, reached_at, vent_closed_at,
            temp_at_start, temp_at_end, trigger_detail, joined_at
           ) VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(cycle_id,room_id) DO UPDATE SET
             target_temp=excluded.target_temp,
             reached_at=excluded.reached_at,
             vent_closed_at=excluded.vent_closed_at,
             temp_at_end=excluded.temp_at_end,
             trigger_detail=COALESCE(excluded.trigger_detail, room_cycle_states.trigger_detail)
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
    )


async def get_room_cycle_states(conn: aiosqlite.Connection, cycle_id: str) -> list[RoomCycleState]:
    async with conn.execute("SELECT * FROM room_cycle_states WHERE cycle_id=?", (cycle_id,)) as cur:
        rows = await cur.fetchall()
    return [_row_to_room_cycle_state(r) for r in rows]


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
            timestamp=_dt(r["timestamp"]),
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
            timestamp=_dt(r["timestamp"]),
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
            timestamp=_dt(r["timestamp"]),
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

    return {
        "start_date": start_date,
        "end_date": end_date,
        "thermostat_entity_id": thermostat_id,
        "heating_seconds": heating,
        "cooling_seconds": cooling,
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
        hours        → {heating_seconds, cooling_seconds}
        cycles       → integer cycle count
        avg_duration → avg cycle duration in seconds
        duty_cycle   → percentage (0–100)
        outside_temp → avg outside_temp_at_start
    """
    if granularity not in ("day", "month"):
        raise ValueError(f"unsupported granularity: {granularity}")
    if metric not in ("hours", "cycles", "avg_duration", "duty_cycle", "outside_temp"):
        raise ValueError(f"unsupported metric: {metric}")

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
            AVG(outside_temp_at_start) AS avg_outside_temp_at_start
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
    return out


def _seconds_in_month(yyyy_mm: str) -> float:
    """Approximate seconds in a YYYY-MM bucket, ignoring DST."""
    year, month = (int(p) for p in yyyy_mm.split("-"))
    next_first = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return (next_first - datetime(year, month, 1)).total_seconds()


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
        total_cycles = int((await cur.fetchone())["n"] or 0)

    sql = """
        SELECT
            r.id AS room_id,
            r.name AS room_name,
            COUNT(rcs.cycle_id) AS participation_count,
            CAST(ROUND(COALESCE(SUM(CASE WHEN cl.mode='heating'
                THEN (julianday(COALESCE(rcs.vent_closed_at, cl.ended_at)) - julianday(COALESCE(rcs.joined_at, cl.started_at))) * 86400.0 END), 0)) AS INTEGER) AS heating_seconds,
            CAST(ROUND(COALESCE(SUM(CASE WHEN cl.mode='cooling'
                THEN (julianday(COALESCE(rcs.vent_closed_at, cl.ended_at)) - julianday(COALESCE(rcs.joined_at, cl.started_at))) * 86400.0 END), 0)) AS INTEGER) AS cooling_seconds,
            AVG(CASE WHEN rcs.reached_at IS NOT NULL
                THEN (julianday(rcs.reached_at) - julianday(COALESCE(rcs.joined_at, cl.started_at))) * 86400.0 END) AS avg_time_to_target_seconds
        FROM rooms r
        LEFT JOIN room_cycle_states rcs ON rcs.room_id = r.id
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
    sql = f"""
        SELECT
            id,
            mode,
            outside_temp_at_start,
            outside_temp_at_end,
            (julianday(ended_at) - julianday(started_at)) * 1440.0 AS duration_minutes,
            started_at
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
        }
        for r in rows
    ]


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
        # Parse as naive UTC, then convert to local for bucketing. We use
        # astimezone(None) which respects the OS-configured local TZ.
        s = datetime.fromisoformat(r["started_at"]).replace(tzinfo=UTC).astimezone()
        e = datetime.fromisoformat(r["ended_at"]).replace(tzinfo=UTC).astimezone()
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
    # Periodic trim — avoid subquery overhead on every insert
    if rowid and rowid % _TRIM_EVERY == 0:
        await conn.execute(
            """DELETE FROM event_log WHERE id <= (
                SELECT MIN(id) FROM (
                    SELECT id FROM event_log ORDER BY id DESC LIMIT ?
                )
            )""",
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
    """Delete event logs older than N days. Returns number of rows deleted."""
    cutoff = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=older_than_days)).isoformat()
    async with conn.execute("DELETE FROM event_log WHERE timestamp < ?", (cutoff,)) as cur:
        count = cur.rowcount or 0
    await conn.commit()
    return count


async def clear_event_logs(conn: aiosqlite.Connection) -> None:
    """Delete all event log rows (manual user-initiated clear)."""
    await conn.execute("DELETE FROM event_log")
    await conn.commit()
