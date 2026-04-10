"""
Room manager: determines which rooms are active for a given thermostat
and at what target temperature, applying priority order:

  1. Active override (not yet expired)
  2. Matching schedule block for current local day/time
  3. Presence holdover active (expires_at > now)
  4. Idle
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite

from ..models import Room, RoomOverride, PresenceHoldoverState, Schedule
from .. import db

log = logging.getLogger(__name__)


@dataclass
class ActiveRoom:
    room: Room
    target_temp: float
    source: str  # 'override' | 'schedule' | 'presence' | 'idle'


async def get_active_rooms(
    conn: aiosqlite.Connection,
    thermostat_entity_id: str,
    now: Optional[datetime] = None,
) -> list[ActiveRoom]:
    """Return all rooms for the given thermostat that should be active right now."""
    if now is None:
        now = datetime.now()  # local time

    rooms = await db.get_rooms_for_thermostat(conn, thermostat_entity_id)
    all_schedules = await db.get_all_schedules(conn)
    schedules_by_room: dict[str, list[Schedule]] = {}
    for s in all_schedules:
        schedules_by_room.setdefault(s.room_id, []).append(s)

    active: list[ActiveRoom] = []
    for room in rooms:
        result = await _resolve_room(conn, room, schedules_by_room.get(room.id, []), now)
        if result.source != "idle":
            active.append(result)
    return active


async def _resolve_room(
    conn: aiosqlite.Connection,
    room: Room,
    schedules: list[Schedule],
    now: datetime,
) -> ActiveRoom:
    # 1. Override
    override = await db.get_room_override(conn, room.id)
    if override and override.expires_at > now:
        return ActiveRoom(room=room, target_temp=override.target_temp, source="override")

    # 2. Schedule
    schedule_target = _matching_schedule(schedules, now)
    if schedule_target is not None:
        return ActiveRoom(room=room, target_temp=schedule_target, source="schedule")

    # 3. Presence holdover
    if room.presence_holdover_hours > 0:
        holdover = await db.get_holdover_state(conn, room.id)
        if holdover and holdover.expires_at > now:
            # Room-level temp takes priority; fall back to thermostat default_temp
            presence_temp = room.system_wide_temp
            if presence_temp is None:
                tc = await db.get_thermostat_config(conn, room.thermostat_entity_id)
                presence_temp = tc.default_temp
            if presence_temp is not None:
                return ActiveRoom(room=room, target_temp=presence_temp, source="presence")
            else:
                log.debug(
                    "Room %s has presence holdover but no presence temp configured "
                    "(set room system_wide_temp or thermostat default_temp) — skipping",
                    room.name,
                )

    return ActiveRoom(room=room, target_temp=0.0, source="idle")


def _matching_schedule(schedules: list[Schedule], now: datetime) -> Optional[float]:
    """Return the target_temp of the first matching schedule block, or None."""
    current_day = now.weekday()  # 0=Monday
    current_time = now.time().replace(second=0, microsecond=0)

    matches = [
        s for s in schedules
        if current_day in s.days_of_week
        and s.start_time <= current_time < s.end_time
    ]
    if not matches:
        return None
    # Prefer earliest start_time on tie
    matches.sort(key=lambda s: s.start_time)
    return matches[0].target_temp


async def handle_presence_event(
    conn: aiosqlite.Connection,
    room: Room,
    now: Optional[datetime] = None,
) -> bool:
    """
    Called when a presence sensor fires for a room.
    Updates/resets the holdover countdown.
    Returns True if the room was newly activated (wasn't already in holdover).
    """
    if room.presence_holdover_hours <= 0:
        return False
    if now is None:
        now = datetime.now()

    existing = await db.get_holdover_state(conn, room.id)
    was_active = existing is not None and existing.expires_at > now

    expires_at = now + timedelta(hours=room.presence_holdover_hours)
    state = PresenceHoldoverState(
        room_id=room.id,
        last_detected_at=now,
        expires_at=expires_at,
    )
    await db.upsert_holdover_state(conn, state)
    log.debug(
        "Presence holdover reset for room %s — expires %s",
        room.name, expires_at.isoformat()
    )
    return not was_active


async def expire_holdovers(
    conn: aiosqlite.Connection,
    now: Optional[datetime] = None,
) -> list[str]:
    """
    Remove expired holdover states. Returns list of room IDs that were removed.
    Call this on every scheduler tick.
    """
    if now is None:
        now = datetime.now()

    all_states = await db.get_all_holdover_states(conn)
    expired_ids: list[str] = []
    for state in all_states:
        if state.expires_at <= now:
            await db.delete_holdover_state(conn, state.room_id)
            expired_ids.append(state.room_id)
            log.info("Presence holdover expired for room_id=%s", state.room_id)
    return expired_ids
