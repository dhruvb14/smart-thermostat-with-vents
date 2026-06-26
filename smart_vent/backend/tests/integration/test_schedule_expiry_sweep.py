"""Integration tests for the schedule self-expiry sweep (Issue #359).

Drives ``Scheduler._sweep_expired_schedules`` against the real DB connection
the integration ``client`` fixture provides.
"""

from __future__ import annotations

from datetime import time, timedelta

import pytest

from backend import db as _db
from backend import tz
from backend.models import Schedule


async def _make_room(client, name: str = "R") -> str:
    resp = await client.post(
        "/api/rooms",
        json={"name": name, "thermostat_entity_id": "climate.test_thermostat"},
    )
    return str((await resp.json())["id"])


def _inactive_block_days(now) -> list[int]:
    """A weekday that is not today, so a block on it is not active now."""
    return [(now.weekday() + 2) % 7]


async def _insert(conn, room_id, *, days, start, end, enabled, expires_at) -> Schedule:
    s = Schedule.create(
        room_id=room_id,
        days_of_week=days,
        start_time=start,
        end_time=end,
        target_temp=68,
        enabled=enabled,
        expires_at=expires_at,
    )
    await _db.upsert_schedule(conn, s)
    return s


@pytest.mark.asyncio
async def test_sweep_disables_expired_idle_schedule(client) -> None:
    room = await _make_room(client)
    conn = client.app["scheduler"]._db_conn
    now = tz.now_local()
    s = await _insert(
        conn,
        room,
        days=_inactive_block_days(now),
        start=time(1, 0),
        end=time(2, 0),
        enabled=True,
        expires_at=now.replace(tzinfo=None) - timedelta(hours=1),
    )
    await client.app["scheduler"]._sweep_expired_schedules()
    got = await _db.get_schedules_for_room(conn, room)
    assert next(g for g in got if g.id == s.id).enabled is False


@pytest.mark.asyncio
async def test_sweep_defers_while_block_active(client) -> None:
    room = await _make_room(client)
    conn = client.app["scheduler"]._db_conn
    now = tz.now_local()
    # An overnight block start==end on today is active for the whole day.
    s = await _insert(
        conn,
        room,
        days=[now.weekday()],
        start=time(0, 0),
        end=time(0, 0),
        enabled=True,
        expires_at=now.replace(tzinfo=None) - timedelta(hours=1),
    )
    await client.app["scheduler"]._sweep_expired_schedules()
    got = await _db.get_schedules_for_room(conn, room)
    # Still enabled — the in-progress block must finish first.
    assert next(g for g in got if g.id == s.id).enabled is True


@pytest.mark.asyncio
async def test_sweep_ignores_never_expire(client) -> None:
    room = await _make_room(client)
    conn = client.app["scheduler"]._db_conn
    now = tz.now_local()
    s = await _insert(
        conn,
        room,
        days=_inactive_block_days(now),
        start=time(1, 0),
        end=time(2, 0),
        enabled=True,
        expires_at=None,
    )
    await client.app["scheduler"]._sweep_expired_schedules()
    got = await _db.get_schedules_for_room(conn, room)
    assert next(g for g in got if g.id == s.id).enabled is True


@pytest.mark.asyncio
async def test_sweep_uses_local_tz_not_utc(client, monkeypatch) -> None:
    """Regression guard: the sweep compares against LOCAL now via tz.py.

    With TZ=America/New_York and an expiry 2h in the *local* future, the
    schedule must NOT be disabled. A buggy implementation comparing against
    UTC now (≈5h ahead) would see it as expired and wrongly disable it.
    """
    monkeypatch.setenv("TZ", "America/New_York")
    room = await _make_room(client)
    conn = client.app["scheduler"]._db_conn
    now = tz.now_local()  # NY local
    s = await _insert(
        conn,
        room,
        days=_inactive_block_days(now),
        start=time(1, 0),
        end=time(2, 0),
        enabled=True,
        expires_at=now.replace(tzinfo=None) + timedelta(hours=2),  # local future
    )
    await client.app["scheduler"]._sweep_expired_schedules()
    got = await _db.get_schedules_for_room(conn, room)
    assert next(g for g in got if g.id == s.id).enabled is True
