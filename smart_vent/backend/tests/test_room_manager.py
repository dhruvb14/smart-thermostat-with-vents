"""
Tests for the room manager module.

Covers:
  - _schedule_active: daytime and overnight schedule matching
  - _find_matching_schedule: best match selection
  - schedules_overlap: interval overlap detection
  - get_active_rooms / _resolve_room: priority resolution
  - handle_presence_event: holdover creation and reset
  - expire_holdovers: cleanup of expired states
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import aiosqlite
import pytest

from backend import db
from backend.engine.room_manager import (
    _find_matching_schedule,
    _schedule_active,
    _seconds_since_schedule_end,
    expire_holdovers,
    get_active_rooms,
    get_overflow_candidates,
    get_room_active_status,
    handle_presence_event,
    schedules_overlap,
)
from backend.models import (
    Room,
    RoomOverride,
    Schedule,
    ThermostatConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

THERMO_ID = "climate.test_thermostat"


def _make_schedule(
    room_id: str = "room1",
    days: list[int] | None = None,
    start: time = time(8, 0),
    end: time = time(17, 0),
    target: float = 74.0,
    sid: str = "sched1",
) -> Schedule:
    if days is None:
        days = [0, 1, 2, 3, 4]  # Mon-Fri
    return Schedule(
        id=sid,
        room_id=room_id,
        days_of_week=days,
        start_time=start,
        end_time=end,
        target_temp=target,
    )


async def _setup_db() -> aiosqlite.Connection:
    """Create an in-memory DB with schema initialized."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    return conn


async def _insert_room(
    conn: aiosqlite.Connection,
    room_id: str = "room1",
    name: str = "Test Room",
    thermo: str = THERMO_ID,
    presence_hours: float = 2.0,
    system_wide_temp: float | None = None,
) -> Room:
    room = Room(
        id=room_id,
        name=name,
        thermostat_entity_id=thermo,
        presence_holdover_hours=presence_hours,
        system_wide_temp=system_wide_temp,
    )
    await db.upsert_room(conn, room)
    return room


# ---------------------------------------------------------------------------
# _schedule_active: daytime blocks
# ---------------------------------------------------------------------------


class TestScheduleActiveDaytime:
    """Normal daytime schedule blocks (end > start)."""

    def test_within_window(self):
        s = _make_schedule(start=time(8, 0), end=time(17, 0), days=[0])
        assert _schedule_active(s, 0, time(12, 0)) is True

    def test_before_window(self):
        s = _make_schedule(start=time(8, 0), end=time(17, 0), days=[0])
        assert _schedule_active(s, 0, time(7, 59)) is False

    def test_at_start_boundary(self):
        s = _make_schedule(start=time(8, 0), end=time(17, 0), days=[0])
        assert _schedule_active(s, 0, time(8, 0)) is True

    def test_at_end_boundary(self):
        """End time is exclusive: [start, end)."""
        s = _make_schedule(start=time(8, 0), end=time(17, 0), days=[0])
        assert _schedule_active(s, 0, time(17, 0)) is False

    def test_wrong_day(self):
        s = _make_schedule(start=time(8, 0), end=time(17, 0), days=[0])  # Monday only
        assert _schedule_active(s, 1, time(12, 0)) is False  # Tuesday


# ---------------------------------------------------------------------------
# _schedule_active: overnight blocks
# ---------------------------------------------------------------------------


class TestScheduleActiveOvernight:
    """Overnight schedule blocks where end_time <= start_time."""

    def test_evening_portion(self):
        """21:00→07:00: active at 23:00 on the scheduled day."""
        s = _make_schedule(start=time(21, 0), end=time(7, 0), days=[0])
        assert _schedule_active(s, 0, time(23, 0)) is True

    def test_morning_portion(self):
        """21:00→07:00: active at 05:00 on the next day (yesterday=Mon is in days)."""
        s = _make_schedule(start=time(21, 0), end=time(7, 0), days=[0])
        # Tuesday (day=1), yesterday=Monday(0) which is in days
        assert _schedule_active(s, 1, time(5, 0)) is True

    def test_morning_wrong_yesterday(self):
        """21:00→07:00: not active at 05:00 if yesterday is not in days."""
        s = _make_schedule(start=time(21, 0), end=time(7, 0), days=[0])
        # Wednesday (day=2), yesterday=Tuesday(1) not in days
        assert _schedule_active(s, 2, time(5, 0)) is False

    def test_gap_between_portions(self):
        """21:00→07:00: not active at 12:00 (daytime gap)."""
        s = _make_schedule(start=time(21, 0), end=time(7, 0), days=[0])
        assert _schedule_active(s, 0, time(12, 0)) is False

    def test_at_end_boundary_overnight(self):
        """End time is exclusive for overnight too."""
        s = _make_schedule(start=time(21, 0), end=time(7, 0), days=[0])
        assert _schedule_active(s, 1, time(7, 0)) is False

    def test_sunday_to_monday_wraparound(self):
        """Sunday 22:00→06:00: morning portion active on Monday."""
        s = _make_schedule(start=time(22, 0), end=time(6, 0), days=[6])  # Sunday
        # Monday (day=0), yesterday=Sunday(6) in days
        assert _schedule_active(s, 0, time(4, 0)) is True


# ---------------------------------------------------------------------------
# _find_matching_schedule
# ---------------------------------------------------------------------------


class TestFindMatchingSchedule:
    def test_returns_matching_schedule(self):
        s = _make_schedule(start=time(8, 0), end=time(17, 0), days=[0])
        now = datetime(2026, 4, 13, 12, 0, tzinfo=UTC)  # Monday
        assert _find_matching_schedule([s], now) == s

    def test_returns_none_when_no_match(self):
        s = _make_schedule(start=time(8, 0), end=time(17, 0), days=[0])
        now = datetime(2026, 4, 14, 12, 0, tzinfo=UTC)  # Tuesday
        assert _find_matching_schedule([s], now) is None

    def test_earliest_start_wins_tiebreak(self):
        """When two schedules overlap, the one with earliest start_time wins."""
        s1 = _make_schedule(start=time(9, 0), end=time(17, 0), days=[0], sid="s1")
        s2 = _make_schedule(start=time(8, 0), end=time(12, 0), days=[0], sid="s2")
        now = datetime(2026, 4, 13, 10, 0, tzinfo=UTC)  # Monday 10:00
        assert _find_matching_schedule([s1, s2], now) == s2

    def test_empty_schedules(self):
        assert _find_matching_schedule([], datetime.now(UTC)) is None


# ---------------------------------------------------------------------------
# schedules_overlap
# ---------------------------------------------------------------------------


class TestSchedulesOverlap:
    def test_overlapping_same_day(self):
        a = _make_schedule(start=time(8, 0), end=time(12, 0), days=[0], sid="a")
        b = _make_schedule(start=time(10, 0), end=time(14, 0), days=[0], sid="b")
        assert schedules_overlap(a, b) is True

    def test_non_overlapping_same_day(self):
        a = _make_schedule(start=time(8, 0), end=time(10, 0), days=[0], sid="a")
        b = _make_schedule(start=time(10, 0), end=time(12, 0), days=[0], sid="b")
        assert schedules_overlap(a, b) is False

    def test_different_days_no_overlap(self):
        a = _make_schedule(start=time(8, 0), end=time(12, 0), days=[0], sid="a")
        b = _make_schedule(start=time(8, 0), end=time(12, 0), days=[1], sid="b")
        assert schedules_overlap(a, b) is False

    def test_overnight_overlap_with_daytime(self):
        """Overnight 22:00→06:00 Mon overlaps with 05:00→08:00 Tue."""
        a = _make_schedule(start=time(22, 0), end=time(6, 0), days=[0], sid="a")
        b = _make_schedule(start=time(5, 0), end=time(8, 0), days=[1], sid="b")  # Tuesday
        assert schedules_overlap(a, b) is True

    def test_overnight_no_overlap(self):
        """Overnight 22:00→06:00 Mon does not overlap with 07:00→09:00 Tue."""
        a = _make_schedule(start=time(22, 0), end=time(6, 0), days=[0], sid="a")
        b = _make_schedule(start=time(7, 0), end=time(9, 0), days=[1], sid="b")
        assert schedules_overlap(a, b) is False


# ---------------------------------------------------------------------------
# get_active_rooms: priority resolution
# ---------------------------------------------------------------------------


class TestGetActiveRooms:
    @pytest.mark.asyncio
    async def test_schedule_activates_room(self):
        conn = await _setup_db()
        await _insert_room(conn, "r1", "Bedroom")

        # Add a schedule for Mon 8:00-17:00
        sched = Schedule.create(
            room_id="r1",
            days_of_week=[0],
            start_time=time(8, 0),
            end_time=time(17, 0),
            target_temp=74.0,
        )
        await db.upsert_schedule(conn, sched)

        now = datetime(2026, 4, 13, 12, 0, tzinfo=UTC)  # Monday noon
        active = await get_active_rooms(conn, THERMO_ID, now=now)
        assert len(active) == 1
        assert active[0].source == "schedule"
        assert active[0].target_temp == 74.0
        await conn.close()

    @pytest.mark.asyncio
    async def test_no_schedule_returns_empty(self):
        conn = await _setup_db()
        await _insert_room(conn, "r1", "Bedroom")

        # Saturday — no schedules defined (default is Mon-Fri)
        now = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)  # Saturday
        active = await get_active_rooms(conn, THERMO_ID, now=now)
        assert len(active) == 0
        await conn.close()

    @pytest.mark.asyncio
    async def test_override_takes_priority_over_schedule(self):
        conn = await _setup_db()
        await _insert_room(conn, "r1", "Bedroom")

        sched = Schedule.create(
            room_id="r1",
            days_of_week=[0],
            start_time=time(8, 0),
            end_time=time(17, 0),
            target_temp=74.0,
        )
        await db.upsert_schedule(conn, sched)

        now = datetime(2026, 4, 13, 12, 0, tzinfo=UTC)  # Monday noon
        override = RoomOverride(
            room_id="r1",
            target_temp=78.0,
            expires_at=now + timedelta(hours=1),
        )
        await db.set_room_override(conn, override)

        active = await get_active_rooms(conn, THERMO_ID, now=now)
        assert len(active) == 1
        assert active[0].source == "override"
        assert active[0].target_temp == 78.0
        await conn.close()

    @pytest.mark.asyncio
    async def test_expired_override_falls_through_to_schedule(self):
        conn = await _setup_db()
        await _insert_room(conn, "r1", "Bedroom")

        sched = Schedule.create(
            room_id="r1",
            days_of_week=[0],
            start_time=time(8, 0),
            end_time=time(17, 0),
            target_temp=74.0,
        )
        await db.upsert_schedule(conn, sched)

        now = datetime(2026, 4, 13, 12, 0, tzinfo=UTC)
        expired_override = RoomOverride(
            room_id="r1",
            target_temp=78.0,
            expires_at=now - timedelta(hours=1),  # already expired
        )
        await db.set_room_override(conn, expired_override)

        active = await get_active_rooms(conn, THERMO_ID, now=now)
        assert len(active) == 1
        assert active[0].source == "schedule"
        await conn.close()

    @pytest.mark.asyncio
    async def test_presence_holdover_activates_room(self):
        conn = await _setup_db()
        await _insert_room(conn, "r1", "Bedroom", system_wide_temp=72.0)

        now = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)  # Saturday, no schedules
        # Create holdover that hasn't expired
        await handle_presence_event(
            conn,
            Room(
                id="r1",
                name="Bedroom",
                thermostat_entity_id=THERMO_ID,
                presence_holdover_hours=2.0,
                system_wide_temp=72.0,
            ),
            now=now,
        )

        check_time = now + timedelta(minutes=30)
        active = await get_active_rooms(conn, THERMO_ID, now=check_time)
        assert len(active) == 1
        assert active[0].source == "presence"
        assert active[0].target_temp == 72.0
        await conn.close()

    @pytest.mark.asyncio
    async def test_presence_without_temp_config_skipped(self):
        """Room with holdover but no system_wide_temp or default_temp → idle."""
        conn = await _setup_db()
        await _insert_room(conn, "r1", "Bedroom", system_wide_temp=None)

        now = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
        room = Room(
            id="r1",
            name="Bedroom",
            thermostat_entity_id=THERMO_ID,
            presence_holdover_hours=2.0,
        )
        await handle_presence_event(conn, room, now=now)

        check_time = now + timedelta(minutes=30)
        active = await get_active_rooms(conn, THERMO_ID, now=check_time)
        assert len(active) == 0  # idle because no temp configured
        await conn.close()

    @pytest.mark.asyncio
    async def test_presence_uses_thermostat_default_temp(self):
        """When room has no system_wide_temp, falls back to thermostat default_temp."""
        conn = await _setup_db()
        await _insert_room(conn, "r1", "Bedroom", system_wide_temp=None)

        # Set thermostat default_temp
        tc = ThermostatConfig(thermostat_entity_id=THERMO_ID, default_temp=70.0)
        await db.upsert_thermostat_config(conn, tc)

        now = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
        room = Room(
            id="r1",
            name="Bedroom",
            thermostat_entity_id=THERMO_ID,
            presence_holdover_hours=2.0,
        )
        await handle_presence_event(conn, room, now=now)

        check_time = now + timedelta(minutes=30)
        active = await get_active_rooms(conn, THERMO_ID, now=check_time)
        assert len(active) == 1
        assert active[0].source == "presence"
        assert active[0].target_temp == 70.0
        await conn.close()


# ---------------------------------------------------------------------------
# handle_presence_event
# ---------------------------------------------------------------------------


class TestHandlePresenceEvent:
    @pytest.mark.asyncio
    async def test_creates_holdover(self):
        conn = await _setup_db()
        room = await _insert_room(conn, "r1", "Bedroom")

        now = datetime(2026, 4, 13, 12, 0, tzinfo=UTC)
        newly_active = await handle_presence_event(conn, room, now=now)
        assert newly_active is True

        holdover = await db.get_holdover_state(conn, "r1")
        assert holdover is not None
        assert holdover.expires_at == now + timedelta(hours=2.0)
        await conn.close()

    @pytest.mark.asyncio
    async def test_resets_holdover_timer(self):
        """Second presence event resets the expiry."""
        conn = await _setup_db()
        room = await _insert_room(conn, "r1", "Bedroom")

        t1 = datetime(2026, 4, 13, 12, 0, tzinfo=UTC)
        await handle_presence_event(conn, room, now=t1)

        t2 = t1 + timedelta(minutes=30)
        newly_active = await handle_presence_event(conn, room, now=t2)
        assert newly_active is False  # already active

        holdover = await db.get_holdover_state(conn, "r1")
        assert holdover.expires_at == t2 + timedelta(hours=2.0)
        await conn.close()

    @pytest.mark.asyncio
    async def test_zero_holdover_hours_noop(self):
        """Room with holdover_hours=0 ignores presence."""
        conn = await _setup_db()
        room = await _insert_room(conn, "r1", "Bedroom", presence_hours=0.0)

        now = datetime(2026, 4, 13, 12, 0, tzinfo=UTC)
        result = await handle_presence_event(conn, room, now=now)
        assert result is False

        holdover = await db.get_holdover_state(conn, "r1")
        assert holdover is None
        await conn.close()


# ---------------------------------------------------------------------------
# expire_holdovers
# ---------------------------------------------------------------------------


class TestExpireHoldovers:
    @pytest.mark.asyncio
    async def test_removes_expired(self):
        conn = await _setup_db()
        room = await _insert_room(conn, "r1", "Bedroom")

        t1 = datetime(2026, 4, 13, 10, 0, tzinfo=UTC)
        await handle_presence_event(conn, room, now=t1)

        # Move time past expiry (2 hours later)
        t2 = t1 + timedelta(hours=3)
        expired = await expire_holdovers(conn, now=t2)
        assert "r1" in expired

        holdover = await db.get_holdover_state(conn, "r1")
        assert holdover is None
        await conn.close()

    @pytest.mark.asyncio
    async def test_keeps_active(self):
        conn = await _setup_db()
        room = await _insert_room(conn, "r1", "Bedroom")

        t1 = datetime(2026, 4, 13, 10, 0, tzinfo=UTC)
        await handle_presence_event(conn, room, now=t1)

        # Only 30 minutes later — still active
        t2 = t1 + timedelta(minutes=30)
        expired = await expire_holdovers(conn, now=t2)
        assert expired == []

        holdover = await db.get_holdover_state(conn, "r1")
        assert holdover is not None
        await conn.close()

    @pytest.mark.asyncio
    async def test_empty_holdovers_noop(self):
        conn = await _setup_db()
        expired = await expire_holdovers(conn)
        assert expired == []
        await conn.close()


# ---------------------------------------------------------------------------
# get_room_active_status — countdowns and next-schedule lookup
# Regression coverage for UTC-aware `now` mixing with naive datetime.combine()
# inside _seconds_until_schedule_end / _next_schedule_start (would previously
# raise "can't subtract offset-naive and offset-aware datetimes").
# ---------------------------------------------------------------------------


class TestGetRoomActiveStatus:
    @pytest.mark.asyncio
    async def test_utc_now_does_not_raise_with_active_schedule(self):
        """UTC-aware now against naive schedule datetimes must not raise."""
        conn = await _setup_db()
        room = await _insert_room(conn, "r1", "Bedroom")

        sched = Schedule.create(
            room_id="r1",
            days_of_week=[0],
            start_time=time(8, 0),
            end_time=time(17, 0),
            target_temp=74.0,
        )
        await db.upsert_schedule(conn, sched)

        now = datetime(2026, 4, 13, 12, 0, tzinfo=UTC)  # Monday noon UTC
        status = await get_room_active_status(conn, room, [sched], now=now)

        assert status["source"] == "schedule"
        assert status["target_temp"] == 74.0
        assert status["ends_in_seconds"] is not None
        assert status["ends_in_seconds"] > 0
        await conn.close()

    @pytest.mark.asyncio
    async def test_next_schedule_populated_when_idle(self):
        """Outside any schedule, next_schedule_* fields must be set, not crash."""
        conn = await _setup_db()
        room = await _insert_room(conn, "r1", "Bedroom")

        sched = Schedule.create(
            room_id="r1",
            days_of_week=[0],  # Monday only
            start_time=time(8, 0),
            end_time=time(17, 0),
            target_temp=74.0,
        )
        await db.upsert_schedule(conn, sched)

        # Sunday 10:00 UTC — no schedule active; next is Monday 08:00
        now = datetime(2026, 4, 12, 10, 0, tzinfo=UTC)
        status = await get_room_active_status(conn, room, [sched], now=now)

        assert status["source"] == "idle"
        assert status["next_schedule_in_seconds"] is not None
        assert status["next_schedule_target"] == 74.0
        assert status["next_schedule_label"] is not None
        await conn.close()

    @pytest.mark.asyncio
    async def test_presence_ends_in_seconds_with_utc_now(self):
        """Presence holdover countdown must work with UTC-aware now."""
        conn = await _setup_db()
        room = await _insert_room(conn, "r1", "Bedroom", system_wide_temp=70.0)

        now = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)  # Saturday, no schedules
        await handle_presence_event(conn, room, now=now)

        check_time = now + timedelta(minutes=30)
        status = await get_room_active_status(conn, room, [], now=check_time)

        assert status["source"] == "presence"
        assert status["target_temp"] == 70.0
        # holdover_hours=2.0, 30 min in → ~90 min left (± tick jitter)
        assert status["ends_in_seconds"] is not None
        assert 5000 < status["ends_in_seconds"] < 5500
        await conn.close()


# ---------------------------------------------------------------------------
# get_overflow_candidates: tiered selection for min-runtime hold (Issue #237)
# ---------------------------------------------------------------------------


class TestGetOverflowCandidates:
    """Surplus-conditioning candidate selection during a cycle's minimum-
    runtime hold. Up to three tiers are tried; the first non-empty wins."""

    async def _setup(
        self,
        *,
        rooms: list[tuple[str, str, float | None]],  # (id, name, system_wide_temp)
        thermostat_default_temp: float | None = None,
    ) -> aiosqlite.Connection:
        conn = await _setup_db()
        # Persist a thermostat config so default_temp fallback works.
        tc = ThermostatConfig(
            thermostat_entity_id=THERMO_ID,
            default_temp=thermostat_default_temp,
            deadband=0.5,
        )
        await db.upsert_thermostat_config(conn, tc)
        for rid, name, swt in rooms:
            r = Room(
                id=rid,
                name=name,
                thermostat_entity_id=THERMO_ID,
                system_wide_temp=swt,
            )
            await db.upsert_room(conn, r)
        return conn

    @staticmethod
    def _temp_map(mapping: dict[str, float | None]):
        def _get(room: Room) -> float | None:
            return mapping.get(room.id)

        return _get

    @pytest.mark.asyncio
    async def test_vacation_short_circuits_to_empty(self):
        conn = await self._setup(
            rooms=[("r1", "Office", 70.0)],
            thermostat_default_temp=None,
        )
        out = await get_overflow_candidates(
            conn,
            THERMO_ID,
            hvac_mode="cooling",
            active_room_ids=set(),
            active_cycle_target_f=68.0,
            deadband_f=0.5,
            in_vacation=True,
            get_avg_temp=self._temp_map({"r1": 75.0}),
        )
        assert out == []
        await conn.close()

    @pytest.mark.asyncio
    async def test_active_rooms_excluded_from_pool(self):
        conn = await self._setup(rooms=[("r1", "Office", 70.0)])
        out = await get_overflow_candidates(
            conn,
            THERMO_ID,
            hvac_mode="cooling",
            active_room_ids={"r1"},
            active_cycle_target_f=68.0,
            deadband_f=0.5,
            in_vacation=False,
            get_avg_temp=self._temp_map({"r1": 75.0}),
        )
        # r1 was active in the cycle — must not appear as overflow.
        assert out == []
        await conn.close()

    @pytest.mark.asyncio
    async def test_tier1_cooling_outside_deadband(self):
        conn = await self._setup(
            rooms=[("r1", "Office", 70.0), ("r2", "Gym", 72.0)],
        )
        out = await get_overflow_candidates(
            conn,
            THERMO_ID,
            hvac_mode="cooling",
            active_room_ids=set(),
            active_cycle_target_f=68.0,
            deadband_f=0.5,
            in_vacation=False,
            # r1 is at 71.0 (> 70.0 + 0.5), r2 at 73.0 (> 72.0 + 0.5).
            get_avg_temp=self._temp_map({"r1": 71.0, "r2": 73.0}),
        )
        assert sorted(c.room.id for c in out) == ["r1", "r2"]
        assert all(c.tier == 1 for c in out)
        await conn.close()

    @pytest.mark.asyncio
    async def test_tier1_heating_mirror(self):
        conn = await self._setup(
            rooms=[("r1", "Office", 70.0)],
        )
        out = await get_overflow_candidates(
            conn,
            THERMO_ID,
            hvac_mode="heating",
            active_room_ids=set(),
            active_cycle_target_f=72.0,
            deadband_f=0.5,
            in_vacation=False,
            # r1 at 68.0 (< 70.0 - 0.5) and < active_cycle_target 72.
            get_avg_temp=self._temp_map({"r1": 68.0}),
        )
        assert [c.room.id for c in out] == ["r1"]
        assert out[0].tier == 1
        await conn.close()

    @pytest.mark.asyncio
    async def test_per_room_deadband_override_reclassifies_tier(self):
        """Issue #305: a room's deadband_override governs its overflow tier
        thresholds, not the thermostat default deadband. A room with a WIDE
        override that sits just past its setpoint is inside its own deadband
        (tier 2), even though the thermostat's narrow default would (wrongly)
        place it outside the deadband (tier 1)."""
        conn = await _setup_db()
        await db.upsert_thermostat_config(
            conn, ThermostatConfig(thermostat_entity_id=THERMO_ID, deadband=0.5)
        )
        room = Room(
            id="r1",
            name="Office",
            thermostat_entity_id=THERMO_ID,
            system_wide_temp=70.0,
            deadband_override=2.0,
        )
        await db.upsert_room(conn, room)
        out = await get_overflow_candidates(
            conn,
            THERMO_ID,
            hvac_mode="cooling",
            active_room_ids=set(),
            active_cycle_target_f=68.0,
            deadband_f=0.5,
            in_vacation=False,
            # 71.0 is past setpoint 70 but inside the room's wide 2.0 deadband
            # (≤ 70 + 2.0). With the thermostat's 0.5 deadband it would be tier 1.
            get_avg_temp=self._temp_map({"r1": 71.0}),
        )
        assert [c.room.id for c in out] == ["r1"]
        assert out[0].tier == 2
        await conn.close()

    @pytest.mark.asyncio
    async def test_per_room_deadband_override_affects_tier3_headroom(self):
        """Issue #305: the tier-3 headroom (distance to the opposite-direction
        trigger) must use the room's own deadband. A wide override gives the room
        positive headroom and makes it eligible where the thermostat default
        would yield negative headroom and exclude it."""
        conn = await _setup_db()
        await db.upsert_thermostat_config(
            conn, ThermostatConfig(thermostat_entity_id=THERMO_ID, deadband=0.5)
        )
        room = Room(
            id="r1",
            name="Office",
            thermostat_entity_id=THERMO_ID,
            system_wide_temp=70.0,
            deadband_override=2.0,
        )
        await db.upsert_room(conn, room)
        out = await get_overflow_candidates(
            conn,
            THERMO_ID,
            hvac_mode="cooling",
            active_room_ids=set(),
            active_cycle_target_f=68.0,
            deadband_f=0.5,
            in_vacation=False,
            # 69.0 is below setpoint 70 (no tier 1/2). Opposite trigger =
            # setpoint - deadband: thermostat 0.5 → 69.5 (headroom -0.5, excluded);
            # room override 2.0 → 68.0 (headroom +1.0, eligible tier 3).
            get_avg_temp=self._temp_map({"r1": 69.0}),
        )
        assert [c.room.id for c in out] == ["r1"]
        assert out[0].tier == 3
        assert out[0].headroom == pytest.approx(1.0)
        await conn.close()

    @pytest.mark.asyncio
    async def test_tier2_when_no_tier1_qualifier(self):
        conn = await self._setup(
            rooms=[("r1", "Office", 70.0)],
        )
        out = await get_overflow_candidates(
            conn,
            THERMO_ID,
            hvac_mode="cooling",
            active_room_ids=set(),
            active_cycle_target_f=68.0,
            deadband_f=0.5,
            in_vacation=False,
            # 70.2: > setpoint 70 but inside deadband (≤ 70 + 0.5). > active target.
            get_avg_temp=self._temp_map({"r1": 70.2}),
        )
        assert [c.room.id for c in out] == ["r1"]
        assert out[0].tier == 2
        await conn.close()

    @pytest.mark.asyncio
    async def test_tier3_picks_room_with_most_headroom(self):
        """Tiers 1 & 2 empty: every non-active room is already at-or-past goal
        in the conditioning direction. Tier 3 picks the room with the most
        headroom before it would trigger the opposite cycle."""
        conn = await self._setup(
            rooms=[
                ("r1", "Office", 70.0),  # at 69.5, headroom = 69.5 - (70 - 0.5) = 0
                ("r2", "Gym", 72.0),  # at 71.6, headroom = 71.6 - 71.5 = 0.1
                ("r3", "Den", 74.0),  # at 73.9, headroom = 73.9 - 73.5 = 0.4
            ],
        )
        out = await get_overflow_candidates(
            conn,
            THERMO_ID,
            hvac_mode="cooling",
            active_room_ids=set(),
            active_cycle_target_f=68.0,
            # active_cycle_target > all room temps, so Tiers 1 & 2 yield nothing.
            deadband_f=0.5,
            in_vacation=False,
            get_avg_temp=self._temp_map({"r1": 69.5, "r2": 71.6, "r3": 73.9}),
        )
        assert [c.room.id for c in out] == ["r3"]
        assert out[0].tier == 3
        assert out[0].headroom is not None
        assert out[0].headroom == pytest.approx(0.4)
        await conn.close()

    @pytest.mark.asyncio
    async def test_tier3_excludes_rooms_past_opposite_trigger(self):
        """A room already at-or-past its opposite-direction trigger must not
        receive more conditioning (would force an opposite cycle)."""
        conn = await self._setup(
            rooms=[
                ("r1", "Office", 70.0),  # at 69.0 — already below heat trigger (69.5)
            ],
        )
        out = await get_overflow_candidates(
            conn,
            THERMO_ID,
            hvac_mode="cooling",
            active_room_ids=set(),
            active_cycle_target_f=68.0,
            deadband_f=0.5,
            in_vacation=False,
            get_avg_temp=self._temp_map({"r1": 69.0}),
        )
        # 69.0 < 70 - 0.5 = 69.5 — no headroom in cooling direction.
        # Tier 4 falls back to empty (caller keeps only active-cycle rooms).
        assert out == []
        await conn.close()

    @pytest.mark.asyncio
    async def test_room_falls_back_to_thermostat_default_temp(self):
        """Room with no per-room setpoint uses the thermostat's global one."""
        conn = await self._setup(
            rooms=[("r1", "Bedroom", None)],
            thermostat_default_temp=70.0,
        )
        out = await get_overflow_candidates(
            conn,
            THERMO_ID,
            hvac_mode="cooling",
            active_room_ids=set(),
            active_cycle_target_f=68.0,
            deadband_f=0.5,
            in_vacation=False,
            get_avg_temp=self._temp_map({"r1": 72.0}),
        )
        assert [c.room.id for c in out] == ["r1"]
        assert out[0].tier == 1
        assert out[0].effective_setpoint == 70.0
        await conn.close()

    @pytest.mark.asyncio
    async def test_room_with_no_setpoint_excluded_from_rankable_tiers(self):
        """Room with no per-room AND no thermostat default cannot rank in
        Tiers 1/2/3 (no setpoint → no goal to compare against)."""
        conn = await self._setup(
            rooms=[("r1", "Mystery", None)],
            thermostat_default_temp=None,
        )
        out = await get_overflow_candidates(
            conn,
            THERMO_ID,
            hvac_mode="cooling",
            active_room_ids=set(),
            active_cycle_target_f=68.0,
            deadband_f=0.5,
            in_vacation=False,
            get_avg_temp=self._temp_map({"r1": 78.0}),
        )
        assert out == []
        await conn.close()

    @pytest.mark.asyncio
    async def test_unreadable_sensor_room_skipped(self):
        """Rooms with no temperature reading cannot be evaluated."""
        conn = await self._setup(rooms=[("r1", "Office", 70.0)])
        out = await get_overflow_candidates(
            conn,
            THERMO_ID,
            hvac_mode="cooling",
            active_room_ids=set(),
            active_cycle_target_f=68.0,
            deadband_f=0.5,
            in_vacation=False,
            get_avg_temp=self._temp_map({"r1": None}),
        )
        assert out == []
        await conn.close()

    @pytest.mark.asyncio
    async def test_room_cooler_than_active_target_falls_through_to_tier3(self):
        """Tier 1/2 require ``room.current_temp > active_cycle_target`` (cooling)
        as a conservative supply-direction proxy. A room that's past its own
        setpoint but already cooler than the active cycle target fails that
        check, but Tier 3 may still pick it via headroom — when nothing
        better exists Tier 3 accepts pushing past goal."""
        conn = await self._setup(rooms=[("r1", "Bedroom", 70.0)])
        out = await get_overflow_candidates(
            conn,
            THERMO_ID,
            hvac_mode="cooling",
            active_room_ids=set(),
            active_cycle_target_f=72.0,
            deadband_f=0.5,
            in_vacation=False,
            # 71 > 70+0.5 (Tier 1 setpoint check passes) BUT 71 < 72
            # (Tier 1 active-target gate fails) — falls through to Tier 3,
            # where headroom = 71 - (70 - 0.5) = 1.5.
            get_avg_temp=self._temp_map({"r1": 71.0}),
        )
        assert [c.room.id for c in out] == ["r1"]
        assert out[0].tier == 3
        assert out[0].headroom == pytest.approx(1.5)
        await conn.close()

    @pytest.mark.asyncio
    async def test_unknown_hvac_mode_returns_empty(self):
        conn = await self._setup(rooms=[("r1", "Office", 70.0)])
        out = await get_overflow_candidates(
            conn,
            THERMO_ID,
            hvac_mode="off",
            active_room_ids=set(),
            active_cycle_target_f=68.0,
            deadband_f=0.5,
            in_vacation=False,
            get_avg_temp=self._temp_map({"r1": 75.0}),
        )
        assert out == []
        await conn.close()


# ---------------------------------------------------------------------------
# _seconds_since_schedule_end (ambient off_schedule_only window — Issue #248)
# ---------------------------------------------------------------------------


class TestSecondsSinceScheduleEnd:
    """Naive-local datetimes are used so the result is timezone-independent."""

    def test_no_schedules_returns_none(self):
        assert _seconds_since_schedule_end([], datetime(2026, 4, 13, 8, 0)) is None

    def test_daytime_block_just_ended(self):
        # Mon 08:00–17:00 block; now Mon 17:30 -> 30 min since it ended.
        sched = _make_schedule(days=[0], start=time(8, 0), end=time(17, 0))
        gap = _seconds_since_schedule_end([sched], datetime(2026, 4, 13, 17, 30))
        assert gap == 30 * 60

    def test_block_still_active_returns_previous_week(self):
        # Mon noon, inside the 08:00–17:00 block: today's end hasn't happened,
        # so the most recent end is last Monday — far outside any short window.
        sched = _make_schedule(days=[0], start=time(8, 0), end=time(17, 0))
        gap = _seconds_since_schedule_end([sched], datetime(2026, 4, 13, 12, 0))
        assert gap is not None
        assert gap > 6 * 24 * 3600  # ~6.8 days

    def test_overnight_block_ends_next_morning(self):
        # Mon 21:00 -> Tue 07:00; now Tue 07:15 -> 15 min since the morning end.
        sched = _make_schedule(days=[0], start=time(21, 0), end=time(7, 0))
        gap = _seconds_since_schedule_end([sched], datetime(2026, 4, 14, 7, 15))
        assert gap == 15 * 60

    def test_picks_smallest_gap_across_blocks(self):
        # Two blocks; the more recently ended one wins.
        morning = _make_schedule(days=[0], start=time(6, 0), end=time(9, 0), sid="m")
        afternoon = _make_schedule(days=[0], start=time(13, 0), end=time(15, 0), sid="a")
        gap = _seconds_since_schedule_end([morning, afternoon], datetime(2026, 4, 13, 15, 10))
        assert gap == 10 * 60
