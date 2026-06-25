"""Integration tests for schedule lifecycle: enable/disable, self-expiry, and
copy-to-rooms (Issue #359)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from backend import db as _db
from backend import tz
from backend.models import Schedule


async def _make_room(client, name: str) -> str:
    resp = await client.post(
        "/api/rooms",
        json={"name": name, "thermostat_entity_id": "climate.test_thermostat"},
    )
    assert resp.status == 201, await resp.text()
    return str((await resp.json())["id"])


async def _add_schedule(client, room_id: str, **overrides) -> tuple[int, dict]:
    body = {
        "days_of_week": [0, 1, 2, 3, 4],
        "start_time": "22:00",
        "end_time": "07:00",
        "target_temp": 68,
    }
    body.update(overrides)
    resp = await client.post(f"/api/rooms/{room_id}/schedules", json=body)
    return resp.status, (await resp.json())


# ── Enable / disable + overlap (disabled frees the slot) ────────────────────


@pytest.mark.asyncio
async def test_create_disabled_block_skips_overlap(client) -> None:
    room = await _make_room(client, "R")
    status, _ = await _add_schedule(client, room)
    assert status == 201
    # A disabled block overlapping the enabled one is allowed — it does not
    # reserve its slot.
    status, body = await _add_schedule(client, room, enabled=False)
    assert status == 201, body
    assert body["enabled"] is False


@pytest.mark.asyncio
async def test_create_overlapping_enabled_rejected(client) -> None:
    room = await _make_room(client, "R")
    await _add_schedule(client, room)
    status, body = await _add_schedule(client, room)
    assert status == 400
    assert "Overlaps" in body["error"]


@pytest.mark.asyncio
async def test_enabled_block_may_overlap_a_disabled_one(client) -> None:
    room = await _make_room(client, "R")
    await _add_schedule(client, room, enabled=False)  # parked 22:00–07:00
    status, body = await _add_schedule(client, room)  # enabled, same slot
    assert status == 201, body


@pytest.mark.asyncio
async def test_enabling_rechecks_overlap(client) -> None:
    room = await _make_room(client, "R")
    await _add_schedule(client, room)  # enabled A
    _, parked = await _add_schedule(client, room, enabled=False)  # parked B (same slot)
    # Re-enabling B must be rejected — A reserves the slot now.
    resp = await client.put(f"/api/rooms/{room}/schedules/{parked['id']}", json={"enabled": True})
    assert resp.status == 400
    assert "Overlaps" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_disable_always_allowed(client) -> None:
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room)
    resp = await client.put(f"/api/rooms/{room}/schedules/{s['id']}", json={"enabled": False})
    assert resp.status == 200
    assert (await resp.json())["enabled"] is False


# ── Self-expiry validation + storage ────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_enabled_with_past_expiry_rejected(client) -> None:
    room = await _make_room(client, "R")
    past = (tz.now_local().replace(tzinfo=None) - timedelta(hours=1)).isoformat()
    status, body = await _add_schedule(client, room, expires_at=past)
    assert status == 400
    assert "future" in body["error"]


@pytest.mark.asyncio
async def test_create_with_future_expiry_round_trips(client) -> None:
    room = await _make_room(client, "R")
    future = (tz.now_local().replace(tzinfo=None) + timedelta(days=3)).replace(microsecond=0)
    status, body = await _add_schedule(client, room, expires_at=future.isoformat())
    assert status == 201, body
    assert body["expires_at"].startswith(future.isoformat()[:16])
    # GET round-trips the stored value.
    got = await (await client.get(f"/api/rooms/{room}/schedules")).json()
    assert got[0]["expires_at"].startswith(future.isoformat()[:16])


@pytest.mark.asyncio
async def test_update_can_clear_expiry(client) -> None:
    room = await _make_room(client, "R")
    future = (tz.now_local().replace(tzinfo=None) + timedelta(days=3)).isoformat()
    _, s = await _add_schedule(client, room, expires_at=future)
    resp = await client.put(f"/api/rooms/{room}/schedules/{s['id']}", json={"expires_at": None})
    assert resp.status == 200
    assert (await resp.json())["expires_at"] is None


@pytest.mark.asyncio
async def test_reenabling_past_expiry_rejected(client) -> None:
    # Insert a parked, already-expired schedule directly, then try to re-enable.
    room = await _make_room(client, "R")
    conn = client.app["scheduler"]._db_conn
    past = tz.now_local().replace(tzinfo=None) - timedelta(hours=1)
    from datetime import time as _time

    sched = Schedule.create(
        room_id=room,
        days_of_week=[0],
        start_time=_time(22, 0),
        end_time=_time(23, 0),
        target_temp=68,
        enabled=False,
        expires_at=past,
    )
    await _db.upsert_schedule(conn, sched)
    resp = await client.put(f"/api/rooms/{room}/schedules/{sched.id}", json={"enabled": True})
    assert resp.status == 400
    assert "past" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_editing_unrelated_field_on_enabled_past_expiry_allowed(client) -> None:
    # An enabled schedule can transiently have a past expiry during the sweep's
    # finish-the-current-block deferral window. Editing an unrelated field
    # (target_temp) without touching enabled/expires_at must still be allowed.
    room = await _make_room(client, "R")
    conn = client.app["scheduler"]._db_conn
    past = tz.now_local().replace(tzinfo=None) - timedelta(hours=1)
    from datetime import time as _time

    sched = Schedule.create(
        room_id=room,
        days_of_week=[0],
        start_time=_time(1, 0),
        end_time=_time(2, 0),
        target_temp=68,
        enabled=True,
        expires_at=past,
    )
    await _db.upsert_schedule(conn, sched)
    resp = await client.put(f"/api/rooms/{room}/schedules/{sched.id}", json={"target_temp": 70})
    assert resp.status == 200, await resp.text()


# ── Copy to other rooms ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_copy_to_multiple_rooms(client) -> None:
    src = await _make_room(client, "Src")
    a = await _make_room(client, "A")
    b = await _make_room(client, "B")
    _, s = await _add_schedule(client, src)
    resp = await client.post(
        f"/api/rooms/{src}/schedules/{s['id']}/copy", json={"target_room_ids": [a, b]}
    )
    assert resp.status == 200
    results = await resp.json()
    assert {r["room_id"] for r in results} == {a, b}
    assert all(r["status"] == "created" for r in results)
    for rid in (a, b):
        got = await (await client.get(f"/api/rooms/{rid}/schedules")).json()
        assert len(got) == 1
        assert got[0]["enabled"] is True
        assert got[0]["expires_at"] is None


@pytest.mark.asyncio
async def test_copy_conflict_lands_disabled(client) -> None:
    src = await _make_room(client, "Src")
    dst = await _make_room(client, "Dst")
    _, s = await _add_schedule(client, src)
    await _add_schedule(client, dst)  # dst already has the same enabled slot
    resp = await client.post(
        f"/api/rooms/{src}/schedules/{s['id']}/copy", json={"target_room_ids": [dst]}
    )
    assert resp.status == 200
    result = (await resp.json())[0]
    assert result["status"] == "created_disabled_conflict"
    assert result["conflict_with"]
    got = await (await client.get(f"/api/rooms/{dst}/schedules")).json()
    copied = next(g for g in got if g["id"] == result["schedule_id"])
    assert copied["enabled"] is False


@pytest.mark.asyncio
async def test_copy_does_not_inherit_expiry(client) -> None:
    src = await _make_room(client, "Src")
    dst = await _make_room(client, "Dst")
    future = (tz.now_local().replace(tzinfo=None) + timedelta(days=3)).isoformat()
    _, s = await _add_schedule(client, src, expires_at=future)
    resp = await client.post(
        f"/api/rooms/{src}/schedules/{s['id']}/copy", json={"target_room_ids": [dst]}
    )
    assert resp.status == 200
    got = await (await client.get(f"/api/rooms/{dst}/schedules")).json()
    assert got[0]["expires_at"] is None


@pytest.mark.asyncio
async def test_copy_to_self_rejected(client) -> None:
    src = await _make_room(client, "Src")
    _, s = await _add_schedule(client, src)
    resp = await client.post(
        f"/api/rooms/{src}/schedules/{s['id']}/copy", json={"target_room_ids": [src]}
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_copy_unknown_room_404(client) -> None:
    src = await _make_room(client, "Src")
    _, s = await _add_schedule(client, src)
    resp = await client.post(
        f"/api/rooms/{src}/schedules/{s['id']}/copy", json={"target_room_ids": ["nope"]}
    )
    assert resp.status == 404


@pytest.mark.asyncio
async def test_copy_empty_targets_rejected(client) -> None:
    src = await _make_room(client, "Src")
    _, s = await _add_schedule(client, src)
    resp = await client.post(
        f"/api/rooms/{src}/schedules/{s['id']}/copy", json={"target_room_ids": []}
    )
    assert resp.status == 400
