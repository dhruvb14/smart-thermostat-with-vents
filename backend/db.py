"""
SQLite database layer using aiosqlite.
All public functions are async and accept an aiosqlite.Connection.
Call `init_db(conn)` once at startup to create tables.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, time
from typing import Optional

import aiosqlite

from .models import (
    CycleLog,
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
    notes TEXT NOT NULL DEFAULT ''
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
    rooms_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS room_cycle_states (
    cycle_id TEXT NOT NULL REFERENCES cycle_logs(id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    target_temp REAL NOT NULL,
    reached_at TEXT,
    vent_closed_at TEXT,
    PRIMARY KEY (cycle_id, room_id)
);

CREATE INDEX IF NOT EXISTS idx_schedules_room ON schedules(room_id);
CREATE INDEX IF NOT EXISTS idx_cycle_logs_thermostat ON cycle_logs(thermostat_entity_id);
CREATE INDEX IF NOT EXISTS idx_cycle_logs_ended ON cycle_logs(ended_at);
"""


async def init_db(conn: aiosqlite.Connection) -> None:
    await conn.executescript(SCHEMA)
    await conn.commit()
    log.info("Database initialised")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dt(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


def _t(s: str) -> time:
    return time.fromisoformat(s)


def _dts(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------

async def get_all_rooms(conn: aiosqlite.Connection) -> list[Room]:
    async with conn.execute("SELECT * FROM rooms ORDER BY name") as cur:
        rows = await cur.fetchall()
    return [_row_to_room(r) for r in rows]


async def get_room(conn: aiosqlite.Connection, room_id: str) -> Optional[Room]:
    async with conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_room(row) if row else None


async def get_rooms_for_thermostat(conn: aiosqlite.Connection, thermostat_entity_id: str) -> list[Room]:
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
    )


async def upsert_room(conn: aiosqlite.Connection, room: Room) -> None:
    await conn.execute(
        """INSERT INTO rooms (id,name,thermostat_entity_id,include_thermostat_sensor,
           system_wide_temp,presence_holdover_hours,notes)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name,
             thermostat_entity_id=excluded.thermostat_entity_id,
             include_thermostat_sensor=excluded.include_thermostat_sensor,
             system_wide_temp=excluded.system_wide_temp,
             presence_holdover_hours=excluded.presence_holdover_hours,
             notes=excluded.notes
        """,
        (
            room.id, room.name, room.thermostat_entity_id,
            int(room.include_thermostat_sensor), room.system_wide_temp,
            room.presence_holdover_hours, room.notes,
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
    await conn.execute("DELETE FROM room_sensors WHERE room_id=? AND entity_id=?", (room_id, entity_id))
    await conn.commit()


async def get_room_vents(conn: aiosqlite.Connection, room_id: str) -> list[RoomVent]:
    async with conn.execute("SELECT * FROM room_vents WHERE room_id=?", (room_id,)) as cur:
        rows = await cur.fetchall()
    return [RoomVent(id=r["id"], room_id=r["room_id"], entity_id=r["entity_id"]) for r in rows]


async def add_room_vent(conn: aiosqlite.Connection, v: RoomVent) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO room_vents(id,room_id,entity_id) VALUES (?,?,?)",
        (v.id, v.room_id, v.entity_id),
    )
    await conn.commit()


async def remove_room_vent(conn: aiosqlite.Connection, room_id: str, entity_id: str) -> None:
    await conn.execute("DELETE FROM room_vents WHERE room_id=? AND entity_id=?", (room_id, entity_id))
    await conn.commit()


async def get_room_presence_sensors(conn: aiosqlite.Connection, room_id: str) -> list[RoomPresenceSensor]:
    async with conn.execute("SELECT * FROM room_presence_sensors WHERE room_id=?", (room_id,)) as cur:
        rows = await cur.fetchall()
    return [RoomPresenceSensor(id=r["id"], room_id=r["room_id"], entity_id=r["entity_id"]) for r in rows]


async def add_room_presence_sensor(conn: aiosqlite.Connection, p: RoomPresenceSensor) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO room_presence_sensors(id,room_id,entity_id) VALUES (?,?,?)",
        (p.id, p.room_id, p.entity_id),
    )
    await conn.commit()


async def remove_room_presence_sensor(conn: aiosqlite.Connection, room_id: str, entity_id: str) -> None:
    await conn.execute(
        "DELETE FROM room_presence_sensors WHERE room_id=? AND entity_id=?", (room_id, entity_id)
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
            s.id, s.room_id, json.dumps(s.days_of_week),
            s.start_time.isoformat(), s.end_time.isoformat(), s.target_temp,
        ),
    )
    await conn.commit()


async def delete_schedule(conn: aiosqlite.Connection, schedule_id: str) -> None:
    await conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
    await conn.commit()


# ---------------------------------------------------------------------------
# Thermostat config
# ---------------------------------------------------------------------------

async def get_thermostat_config(
    conn: aiosqlite.Connection, entity_id: str
) -> ThermostatConfig:
    async with conn.execute(
        "SELECT * FROM thermostat_configs WHERE thermostat_entity_id=?", (entity_id,)
    ) as cur:
        row = await cur.fetchone()
    if row:
        return _row_to_tc(row)
    return ThermostatConfig(thermostat_entity_id=entity_id)


async def get_all_thermostat_configs(conn: aiosqlite.Connection) -> list[ThermostatConfig]:
    async with conn.execute("SELECT * FROM thermostat_configs") as cur:
        rows = await cur.fetchall()
    return [_row_to_tc(r) for r in rows]


def _row_to_tc(row) -> ThermostatConfig:
    return ThermostatConfig(
        thermostat_entity_id=row["thermostat_entity_id"],
        min_setpoint=row["min_setpoint"],
        max_setpoint=row["max_setpoint"],
        deadband=row["deadband"],
        max_vent_closed_min=row["max_vent_closed_min"],
        min_open_vents=row["min_open_vents"],
        overshoot_delta=row["overshoot_delta"],
        cycle_timeout_hours=row["cycle_timeout_hours"],
    )


async def upsert_thermostat_config(conn: aiosqlite.Connection, tc: ThermostatConfig) -> None:
    await conn.execute(
        """INSERT INTO thermostat_configs
           (thermostat_entity_id,min_setpoint,max_setpoint,deadband,
            max_vent_closed_min,min_open_vents,overshoot_delta,cycle_timeout_hours)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(thermostat_entity_id) DO UPDATE SET
             min_setpoint=excluded.min_setpoint,
             max_setpoint=excluded.max_setpoint,
             deadband=excluded.deadband,
             max_vent_closed_min=excluded.max_vent_closed_min,
             min_open_vents=excluded.min_open_vents,
             overshoot_delta=excluded.overshoot_delta,
             cycle_timeout_hours=excluded.cycle_timeout_hours
        """,
        (
            tc.thermostat_entity_id, tc.min_setpoint, tc.max_setpoint, tc.deadband,
            tc.max_vent_closed_min, tc.min_open_vents, tc.overshoot_delta, tc.cycle_timeout_hours,
        ),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# Room overrides
# ---------------------------------------------------------------------------

async def get_room_override(conn: aiosqlite.Connection, room_id: str) -> Optional[RoomOverride]:
    async with conn.execute(
        "SELECT * FROM room_overrides WHERE room_id=?", (room_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return RoomOverride(
        room_id=row["room_id"],
        target_temp=row["target_temp"],
        expires_at=datetime.fromisoformat(row["expires_at"]),
    )


async def set_room_override(conn: aiosqlite.Connection, override: RoomOverride) -> None:
    await conn.execute(
        """INSERT INTO room_overrides(room_id,target_temp,expires_at) VALUES(?,?,?)
           ON CONFLICT(room_id) DO UPDATE SET
             target_temp=excluded.target_temp, expires_at=excluded.expires_at
        """,
        (override.room_id, override.target_temp, override.expires_at.isoformat()),
    )
    await conn.commit()


async def clear_room_override(conn: aiosqlite.Connection, room_id: str) -> None:
    await conn.execute("DELETE FROM room_overrides WHERE room_id=?", (room_id,))
    await conn.commit()


async def clear_expired_overrides(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        "DELETE FROM room_overrides WHERE expires_at < ?", (datetime.utcnow().isoformat(),)
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# Presence holdover state
# ---------------------------------------------------------------------------

async def get_all_holdover_states(conn: aiosqlite.Connection) -> list[PresenceHoldoverState]:
    async with conn.execute("SELECT * FROM presence_holdover_state") as cur:
        rows = await cur.fetchall()
    return [
        PresenceHoldoverState(
            room_id=r["room_id"],
            last_detected_at=datetime.fromisoformat(r["last_detected_at"]),
            expires_at=datetime.fromisoformat(r["expires_at"]),
        )
        for r in rows
    ]


async def get_holdover_state(
    conn: aiosqlite.Connection, room_id: str
) -> Optional[PresenceHoldoverState]:
    async with conn.execute(
        "SELECT * FROM presence_holdover_state WHERE room_id=?", (room_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return PresenceHoldoverState(
        room_id=row["room_id"],
        last_detected_at=datetime.fromisoformat(row["last_detected_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
    )


async def upsert_holdover_state(conn: aiosqlite.Connection, state: PresenceHoldoverState) -> None:
    await conn.execute(
        """INSERT INTO presence_holdover_state(room_id,last_detected_at,expires_at)
           VALUES(?,?,?)
           ON CONFLICT(room_id) DO UPDATE SET
             last_detected_at=excluded.last_detected_at,
             expires_at=excluded.expires_at
        """,
        (state.room_id, state.last_detected_at.isoformat(), state.expires_at.isoformat()),
    )
    await conn.commit()


async def delete_holdover_state(conn: aiosqlite.Connection, room_id: str) -> None:
    await conn.execute("DELETE FROM presence_holdover_state WHERE room_id=?", (room_id,))
    await conn.commit()


# ---------------------------------------------------------------------------
# Cycle logs
# ---------------------------------------------------------------------------

async def insert_cycle_log(conn: aiosqlite.Connection, log_: CycleLog) -> None:
    await conn.execute(
        "INSERT INTO cycle_logs(id,thermostat_entity_id,started_at,ended_at,mode,rooms_json) VALUES(?,?,?,?,?,?)",
        (
            log_.id, log_.thermostat_entity_id, log_.started_at.isoformat(),
            _dts(log_.ended_at), log_.mode, log_.rooms_json,
        ),
    )
    await conn.commit()


async def close_cycle_log(conn: aiosqlite.Connection, cycle_id: str, ended_at: datetime) -> None:
    await conn.execute(
        "UPDATE cycle_logs SET ended_at=? WHERE id=?", (ended_at.isoformat(), cycle_id)
    )
    await conn.commit()


async def get_cycle_logs(
    conn: aiosqlite.Connection, limit: int = 50
) -> list[CycleLog]:
    async with conn.execute(
        "SELECT * FROM cycle_logs ORDER BY started_at DESC LIMIT ?", (limit,)
    ) as cur:
        rows = await cur.fetchall()
    return [
        CycleLog(
            id=r["id"],
            thermostat_entity_id=r["thermostat_entity_id"],
            started_at=datetime.fromisoformat(r["started_at"]),
            mode=r["mode"],
            rooms_json=r["rooms_json"],
            ended_at=_dt(r["ended_at"]),
        )
        for r in rows
    ]


async def upsert_room_cycle_state(conn: aiosqlite.Connection, rcs: RoomCycleState) -> None:
    await conn.execute(
        """INSERT INTO room_cycle_states(cycle_id,room_id,target_temp,reached_at,vent_closed_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(cycle_id,room_id) DO UPDATE SET
             reached_at=excluded.reached_at,
             vent_closed_at=excluded.vent_closed_at
        """,
        (rcs.cycle_id, rcs.room_id, rcs.target_temp, _dts(rcs.reached_at), _dts(rcs.vent_closed_at)),
    )
    await conn.commit()


async def get_room_cycle_states(
    conn: aiosqlite.Connection, cycle_id: str
) -> list[RoomCycleState]:
    async with conn.execute(
        "SELECT * FROM room_cycle_states WHERE cycle_id=?", (cycle_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [
        RoomCycleState(
            cycle_id=r["cycle_id"],
            room_id=r["room_id"],
            target_temp=r["target_temp"],
            reached_at=_dt(r["reached_at"]),
            vent_closed_at=_dt(r["vent_closed_at"]),
        )
        for r in rows
    ]
