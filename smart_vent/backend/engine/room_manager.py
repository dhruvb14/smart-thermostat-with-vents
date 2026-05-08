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
from datetime import UTC, datetime, time, timedelta

import aiosqlite

from .. import db
from ..models import PresenceHoldoverState, Room, Schedule

log = logging.getLogger(__name__)

DAYS_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class ActiveRoom:
    room: Room
    target_temp: float
    source: str  # 'override' | 'schedule' | 'presence' | 'idle'


async def get_active_rooms(
    conn: aiosqlite.Connection,
    thermostat_entity_id: str,
    now: datetime | None = None,
) -> list[ActiveRoom]:
    """Return all rooms for the given thermostat that should be active right now."""
    if now is None:
        now = datetime.now(UTC)

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
    match = _find_matching_schedule(schedules, now)
    if match is not None:
        return ActiveRoom(room=room, target_temp=match.target_temp, source="schedule")

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


# ---------------------------------------------------------------------------
# Schedule matching helpers (handle overnight blocks)
# ---------------------------------------------------------------------------


def _schedule_active(s: Schedule, current_day: int, current_time: time) -> bool:
    """
    Return True if the schedule is active at the given (weekday, time).
    Handles overnight blocks where end_time < start_time, e.g. 21:00→07:00.
    """
    is_overnight = s.end_time <= s.start_time  # end next day

    if not is_overnight:
        # Normal daytime block: active on days in days_of_week during [start, end)
        return current_day in s.days_of_week and s.start_time <= current_time < s.end_time
    else:
        # Overnight block spans midnight:
        # - First portion: [start, 24:00) on the scheduled day
        # - Second portion: [00:00, end) on the NEXT day
        yesterday = (current_day - 1) % 7
        in_first_portion = current_day in s.days_of_week and current_time >= s.start_time
        in_second_portion = yesterday in s.days_of_week and current_time < s.end_time
        return in_first_portion or in_second_portion


def _find_matching_schedule(schedules: list[Schedule], now: datetime) -> Schedule | None:
    """Return the best matching schedule block for the current moment, or None."""
    local_now = now.astimezone()  # UTC-aware → local-aware; naive → treated as local
    current_day = local_now.weekday()  # 0=Monday
    current_time = local_now.time().replace(second=0, microsecond=0)

    matches = [s for s in schedules if _schedule_active(s, current_day, current_time)]
    if not matches:
        return None
    # Prefer earliest start_time as tiebreak
    matches.sort(key=lambda s: s.start_time)
    return matches[0]


def _matching_schedule(schedules: list[Schedule], now: datetime) -> float | None:
    """Return target_temp of the first matching schedule block, or None."""
    match = _find_matching_schedule(schedules, now)
    return match.target_temp if match else None


# ---------------------------------------------------------------------------
# Schedule interval overlap
# ---------------------------------------------------------------------------


def _schedule_intervals(s: Schedule) -> list[tuple[int, int, int]]:
    """
    Decompose a schedule into (weekday, start_min, end_min) intervals.
    Overnight schedules (end <= start) produce two intervals: one to midnight,
    one from midnight on the next weekday.
    """
    sm = s.start_time.hour * 60 + s.start_time.minute
    em = s.end_time.hour * 60 + s.end_time.minute
    is_overnight = em <= sm

    intervals: list[tuple[int, int, int]] = []
    for d in s.days_of_week:
        if not is_overnight:
            intervals.append((d, sm, em))
        else:
            intervals.append((d, sm, 1440))  # start → midnight
            intervals.append(((d + 1) % 7, 0, em))  # midnight → end (next day)
    return intervals


def schedules_overlap(a: Schedule, b: Schedule) -> bool:
    """Return True if two schedules share any (day, time) slot."""
    a_intervals = _schedule_intervals(a)
    b_intervals = _schedule_intervals(b)
    for ad, as_, ae in a_intervals:
        for bd, bs, be in b_intervals:
            if ad == bd and as_ < be and bs < ae:
                return True
    return False


# ---------------------------------------------------------------------------
# Detailed room status (for UI)
# ---------------------------------------------------------------------------


def _seconds_until_schedule_end(s: Schedule, now: datetime) -> float:
    """Return seconds until the currently-active block of schedule s ends."""
    # Schedules are stored as wall-clock / local-time. `datetime.combine(...)`
    # below produces a naive datetime, so normalize `now` to naive-local too —
    # subtracting a naive from a tz-aware datetime raises TypeError.
    local_now = now.astimezone().replace(tzinfo=None) if now.tzinfo else now
    current_time = local_now.time()
    today = local_now.date()

    is_overnight = s.end_time <= s.start_time
    if not is_overnight:
        end_dt = datetime.combine(today, s.end_time)
    else:
        if current_time >= s.start_time:
            # Evening portion → ends tomorrow at end_time
            end_dt = datetime.combine(today + timedelta(days=1), s.end_time)
        else:
            # Morning portion → ends today at end_time
            end_dt = datetime.combine(today, s.end_time)

    return max(0.0, (end_dt - local_now).total_seconds())


def _next_schedule_start(
    schedules: list[Schedule],
    now: datetime,
    exclude_id: str | None = None,
) -> tuple[int, float, str] | None:
    """
    Find the soonest upcoming schedule start across all schedules (excluding
    the one with id==exclude_id).

    Returns (seconds_until_start, target_temp, label) or None.
    Label example: "Mon 10:00 PM"
    """
    if not schedules:
        return None

    # Schedules are local wall-clock; do all arithmetic in naive-local so the
    # naive datetime.combine(...) below lines up with `now`.
    local_now = now.astimezone().replace(tzinfo=None) if now.tzinfo else now

    candidates: list[tuple[float, float, str]] = []

    for s in schedules:
        if exclude_id and s.id == exclude_id:
            continue
        # Look ahead up to 7 days (day_offset=0 is today, 6 is 6 days from now)
        for day_offset in range(7):
            candidate_date = local_now.date() + timedelta(days=day_offset)
            candidate_weekday = candidate_date.weekday()
            if candidate_weekday not in s.days_of_week:
                continue
            start_dt = datetime.combine(candidate_date, s.start_time)
            if start_dt <= local_now:
                continue  # already passed
            secs = (start_dt - local_now).total_seconds()
            h = s.start_time.hour
            m = s.start_time.minute
            am_pm = "AM" if h < 12 else "PM"
            h12 = h % 12 or 12
            label = f"{DAYS_SHORT[candidate_weekday]} {h12}:{m:02d} {am_pm}"
            candidates.append((secs, s.target_temp, label))
            break  # take only the soonest occurrence of this schedule

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    secs, target, label = candidates[0]
    return (int(secs), target, label)


async def get_room_active_status(
    conn: aiosqlite.Connection,
    room: Room,
    schedules: list[Schedule],
    now: datetime | None = None,
) -> dict:
    """
    Return detailed active status for a single room, suitable for the UI.

    Shape:
    {
        "room_id": str,
        "source": "schedule" | "presence" | "override" | "idle",
        "target_temp": float | None,
        "ends_in_seconds": int | None,
        "next_schedule_in_seconds": int | None,
        "next_schedule_target": float | None,
        "next_schedule_label": str | None,
    }
    """
    if now is None:
        now = datetime.now(UTC)

    resolved = await _resolve_room(conn, room, schedules, now)

    ends_in_seconds: int | None = None
    current_schedule_id: str | None = None

    holdover = await db.get_holdover_state(conn, room.id)
    presence_holdover_active = holdover is not None and holdover.expires_at > now

    if resolved.source == "override":
        override = await db.get_room_override(conn, room.id)
        if override:
            ends_in_seconds = max(0, int((override.expires_at - now).total_seconds()))

    elif resolved.source == "schedule":
        matching = _find_matching_schedule(schedules, now)
        if matching:
            current_schedule_id = matching.id
            ends_in_seconds = int(_seconds_until_schedule_end(matching, now))

    elif resolved.source == "presence":
        if holdover:
            ends_in_seconds = max(0, int((holdover.expires_at - now).total_seconds()))

    # Find next upcoming schedule
    next_sched = _next_schedule_start(schedules, now, exclude_id=current_schedule_id)

    return {
        "room_id": room.id,
        "source": resolved.source,
        "target_temp": resolved.target_temp if resolved.source != "idle" else None,
        "ends_in_seconds": ends_in_seconds,
        "presence_holdover_active": presence_holdover_active,
        "next_schedule_in_seconds": next_sched[0] if next_sched else None,
        "next_schedule_target": next_sched[1] if next_sched else None,
        "next_schedule_label": next_sched[2] if next_sched else None,
    }


# ---------------------------------------------------------------------------
# Presence handling
# ---------------------------------------------------------------------------


async def handle_presence_event(
    conn: aiosqlite.Connection,
    room: Room,
    now: datetime | None = None,
) -> bool:
    """
    Called when a presence sensor fires for a room.
    Updates/resets the holdover countdown.
    Returns True if the room was newly activated (wasn't already in holdover).
    """
    if room.presence_holdover_hours <= 0:
        return False
    if now is None:
        now = datetime.now(UTC)

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
        room.name,
        expires_at.isoformat(),
    )
    return not was_active


async def expire_holdovers(
    conn: aiosqlite.Connection,
    now: datetime | None = None,
) -> list[str]:
    """
    Remove expired holdover states. Returns list of room IDs that were removed.
    Call this on every scheduler tick.
    """
    if now is None:
        now = datetime.now(UTC)

    all_states = await db.get_all_holdover_states(conn)
    expired_ids: list[str] = []
    for state in all_states:
        if state.expires_at <= now:
            await db.delete_holdover_state(conn, state.room_id)
            expired_ids.append(state.room_id)
            log.info("Presence holdover expired for room_id=%s", state.room_id)
    return expired_ids
