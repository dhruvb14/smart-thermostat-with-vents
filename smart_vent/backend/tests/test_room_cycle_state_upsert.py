"""
Tests for upsert_room_cycle_state ON CONFLICT semantics (Issue #300).

When a room that was opened as overflow (role='overflow') is later promoted to a
full active participant, the engine re-inserts a RoomCycleState with the default
role='active'. The ON CONFLICT(cycle_id, room_id) update must:
  * update `role` (so the promoted room is no longer mislabeled overflow), and
  * preserve the original `joined_at` (COALESCE), only setting it when previously
    NULL.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

from backend import db
from backend.models import CycleLog, Room, RoomCycleState


async def _fresh_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    return conn


async def _seed_cycle(conn: aiosqlite.Connection, cycle_id: str = "c1") -> None:
    await db.upsert_room(conn, Room(id="r1", name="Bedroom", thermostat_entity_id="climate.t"))
    log_ = CycleLog(
        id=cycle_id,
        thermostat_entity_id="climate.t",
        started_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        mode="cooling",
        rooms_json="{}",
    )
    await db.insert_cycle_log(conn, log_)


async def _get_rcs(conn: aiosqlite.Connection, cycle_id: str, room_id: str) -> RoomCycleState:
    states = await db.get_room_cycle_states(conn, cycle_id)
    return next(s for s in states if s.room_id == room_id)


class TestUpsertRoomCycleStateConflict:
    async def test_conflict_updates_role_overflow_to_active(self) -> None:
        conn = await _fresh_db()
        try:
            await _seed_cycle(conn)
            t1 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
            await db.upsert_room_cycle_state(
                conn,
                RoomCycleState(
                    cycle_id="c1",
                    room_id="r1",
                    target_temp=70.0,
                    joined_at=t1,
                    temp_at_start=75.0,
                    role="overflow",
                ),
            )
            # Engine promotes the room to active mid-cycle: a fresh active state
            # is upserted onto the existing overflow row.
            await db.upsert_room_cycle_state(
                conn,
                RoomCycleState(
                    cycle_id="c1",
                    room_id="r1",
                    target_temp=72.0,
                    joined_at=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
                    role="active",
                ),
            )
            rcs = await _get_rcs(conn, "c1", "r1")
            assert rcs.role == "active"
            # temp_at_start is intentionally preserved (overflow re-open path).
            assert rcs.temp_at_start == 75.0
        finally:
            await conn.close()

    async def test_conflict_preserves_original_joined_at(self) -> None:
        conn = await _fresh_db()
        try:
            await _seed_cycle(conn)
            t1 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
            t2 = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
            await db.upsert_room_cycle_state(
                conn,
                RoomCycleState(
                    cycle_id="c1", room_id="r1", target_temp=70.0, joined_at=t1, role="overflow"
                ),
            )
            await db.upsert_room_cycle_state(
                conn,
                RoomCycleState(
                    cycle_id="c1", room_id="r1", target_temp=72.0, joined_at=t2, role="active"
                ),
            )
            rcs = await _get_rcs(conn, "c1", "r1")
            # Original join time is kept, not overwritten by the promotion's time.
            assert rcs.joined_at == t1
        finally:
            await conn.close()

    async def test_conflict_sets_joined_at_when_previously_null(self) -> None:
        conn = await _fresh_db()
        try:
            await _seed_cycle(conn)
            # Room present at cycle start has joined_at=None.
            await db.upsert_room_cycle_state(
                conn,
                RoomCycleState(
                    cycle_id="c1", room_id="r1", target_temp=72.0, joined_at=None, role="active"
                ),
            )
            t2 = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
            await db.upsert_room_cycle_state(
                conn,
                RoomCycleState(
                    cycle_id="c1", room_id="r1", target_temp=72.0, joined_at=t2, role="active"
                ),
            )
            rcs = await _get_rcs(conn, "c1", "r1")
            assert rcs.joined_at == t2
        finally:
            await conn.close()


class TestEcoColumnsSurviveConflict:
    """#420: the ON CONFLICT clause is the #300 failure shape — a column
    dropped from DO UPDATE SET silently keeps stale values. The Eco
    measurability columns are written on the conflict path by the mid-cycle
    in-place update; pin that an upsert onto an existing row overwrites them."""

    async def test_eco_fields_overwritten_on_conflict(self) -> None:
        conn = await _fresh_db()
        try:
            await _seed_cycle(conn)
            await db.upsert_room_cycle_state(
                conn,
                RoomCycleState(
                    cycle_id="c1",
                    room_id="r1",
                    target_temp=70.0,
                    requested_target=70.0,
                    effective_target=70.0,
                    eco_active=False,
                ),
            )
            # Mid-cycle in-place update: Eco relaxed the target.
            await db.upsert_room_cycle_state(
                conn,
                RoomCycleState(
                    cycle_id="c1",
                    room_id="r1",
                    target_temp=74.0,
                    requested_target=70.0,
                    effective_target=74.0,
                    eco_active=True,
                ),
            )
            rcs = await _get_rcs(conn, "c1", "r1")
            assert rcs.requested_target == 70.0
            assert rcs.effective_target == 74.0
            assert rcs.eco_active is True
        finally:
            await conn.close()
