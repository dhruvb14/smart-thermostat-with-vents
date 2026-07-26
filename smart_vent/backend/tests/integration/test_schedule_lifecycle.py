"""Integration tests for schedule lifecycle: enable/disable, self-expiry, and
copy-to-rooms (Issue #359), plus the per-block deadband override (#517) and the
optional display name (#520) on the same write boundary."""

from __future__ import annotations

from datetime import timedelta

import pytest

from backend import db as _db
from backend import schedule_rules, tz
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
async def test_copy_from_disabled_source_creates_enabled_copy(client) -> None:
    # The copy is always created enabled (#359), even when the source is parked.
    src = await _make_room(client, "Src")
    dst = await _make_room(client, "Dst")
    _, s = await _add_schedule(client, src, enabled=False)
    resp = await client.post(
        f"/api/rooms/{src}/schedules/{s['id']}/copy", json={"target_room_ids": [dst]}
    )
    assert resp.status == 200
    assert (await resp.json())[0]["status"] == "created"
    got = await (await client.get(f"/api/rooms/{dst}/schedules")).json()
    assert got[0]["enabled"] is True


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


# ── Per-schedule deadband override (Issue #517) ─────────────────────────────
#
# The band is a nullable DELTA on the write boundary, validated by the same
# `_validate_deadband_override` the per-room field (#277) uses: null clears,
# booleans and non-numerics are rejected, and the converted °F value must land
# in 0–10 inclusive. Create uses "absent → None"; update uses a presence
# sentinel so an omitted key preserves the stored value.


async def _band_of(client, room_id: str, schedule_id: str) -> float | None:
    got = await (await client.get(f"/api/rooms/{room_id}/schedules")).json()
    block = next(g for g in got if g["id"] == schedule_id)
    band: float | None = block["deadband_override"]
    return band


# — create —


@pytest.mark.asyncio
async def test_create_with_band_stores_it(client) -> None:
    room = await _make_room(client, "R")
    status, body = await _add_schedule(client, room, deadband_override=2.5)
    assert status == 201, body
    assert body["deadband_override"] == 2.5
    assert await _band_of(client, room, body["id"]) == 2.5


@pytest.mark.asyncio
async def test_create_with_explicit_null_stores_none(client) -> None:
    room = await _make_room(client, "R")
    status, body = await _add_schedule(client, room, deadband_override=None)
    assert status == 201, body
    assert body["deadband_override"] is None


@pytest.mark.asyncio
async def test_create_with_key_absent_stores_none(client) -> None:
    """The default is inherit — a client that has never heard of the field
    keeps working exactly as before #517."""
    room = await _make_room(client, "R")
    status, body = await _add_schedule(client, room)
    assert status == 201, body
    assert body["deadband_override"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("band", [0, 0.0, 10, 10.0, 5.5])
async def test_create_accepts_values_inside_the_band(client, band) -> None:
    """0 and 10 °F are INCLUSIVE bounds; 0 is a real exact-match band, not
    "unset"."""
    room = await _make_room(client, "R")
    status, body = await _add_schedule(client, room, deadband_override=band)
    assert status == 201, body
    assert body["deadband_override"] == band


@pytest.mark.asyncio
@pytest.mark.parametrize("band", [10.01, -0.1, -1, 25, 1000])
async def test_create_rejects_out_of_range_band(client, band) -> None:
    room = await _make_room(client, "R")
    status, body = await _add_schedule(client, room, deadband_override=band)
    assert status == 400, body
    assert "deadband_override" in body["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("band", ["abc", "", [], {}, [2.0]])
async def test_create_rejects_non_numeric_band(client, band) -> None:
    room = await _make_room(client, "R")
    status, body = await _add_schedule(client, room, deadband_override=band)
    assert status == 400, body


@pytest.mark.asyncio
@pytest.mark.parametrize("band", [True, False])
async def test_create_rejects_boolean_band(client, band) -> None:
    """`bool` is a subclass of `int` in Python — True must NOT slip through as
    a 1°F band (nor False as 0)."""
    room = await _make_room(client, "R")
    status, body = await _add_schedule(client, room, deadband_override=band)
    assert status == 400, body
    assert "must be a number or null" in body["error"]


@pytest.mark.asyncio
async def test_create_rejecting_the_band_creates_nothing(client) -> None:
    room = await _make_room(client, "R")
    status, _ = await _add_schedule(client, room, deadband_override=99)
    assert status == 400
    assert await (await client.get(f"/api/rooms/{room}/schedules")).json() == []


# — update (partial-update semantics) —


@pytest.mark.asyncio
async def test_update_sets_the_band(client) -> None:
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room)
    resp = await client.put(
        f"/api/rooms/{room}/schedules/{s['id']}", json={"deadband_override": 3.0}
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["deadband_override"] == 3.0
    assert await _band_of(client, room, s["id"]) == 3.0


@pytest.mark.asyncio
async def test_update_with_explicit_null_clears_the_band(client) -> None:
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room, deadband_override=3.0)
    resp = await client.put(
        f"/api/rooms/{room}/schedules/{s['id']}", json={"deadband_override": None}
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["deadband_override"] is None
    assert await _band_of(client, room, s["id"]) is None


@pytest.mark.asyncio
async def test_update_with_the_key_omitted_preserves_the_band(client) -> None:
    """The presence sentinel: editing an unrelated field must not silently
    wipe the band. Omitted ≠ null."""
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room, deadband_override=3.0)
    resp = await client.put(f"/api/rooms/{room}/schedules/{s['id']}", json={"target_temp": 71})
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["target_temp"] == 71
    assert body["deadband_override"] == 3.0
    assert await _band_of(client, room, s["id"]) == 3.0


@pytest.mark.asyncio
async def test_update_with_empty_body_preserves_the_band(client) -> None:
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room, deadband_override=3.0)
    resp = await client.put(f"/api/rooms/{room}/schedules/{s['id']}", json={})
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["deadband_override"] == 3.0


@pytest.mark.asyncio
async def test_update_can_set_a_band_on_a_block_that_had_none(client) -> None:
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room)
    assert s["deadband_override"] is None
    resp = await client.put(f"/api/rooms/{room}/schedules/{s['id']}", json={"deadband_override": 0})
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["deadband_override"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("band", [10.01, -0.1, "abc", True, [], {}])
async def test_update_rejects_bad_band_and_leaves_the_stored_value(client, band) -> None:
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room, deadband_override=4.0)
    resp = await client.put(
        f"/api/rooms/{room}/schedules/{s['id']}", json={"deadband_override": band}
    )
    assert resp.status == 400
    # The rejected request must not have mutated anything.
    assert await _band_of(client, room, s["id"]) == 4.0


@pytest.mark.asyncio
async def test_update_rejecting_the_band_does_not_apply_other_fields(client) -> None:
    """Validation runs before the write, so a payload carrying a good
    target_temp and a bad band changes neither."""
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room, deadband_override=4.0)
    resp = await client.put(
        f"/api/rooms/{room}/schedules/{s['id']}",
        json={"target_temp": 71, "deadband_override": 99},
    )
    assert resp.status == 400
    got = await (await client.get(f"/api/rooms/{room}/schedules")).json()
    assert got[0]["target_temp"] == 68
    assert got[0]["deadband_override"] == 4.0


@pytest.mark.asyncio
async def test_band_survives_enable_disable_round_trip(client) -> None:
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room, deadband_override=2.0)
    await client.put(f"/api/rooms/{room}/schedules/{s['id']}", json={"enabled": False})
    resp = await client.put(f"/api/rooms/{room}/schedules/{s['id']}", json={"enabled": True})
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["deadband_override"] == 2.0


# — copy —


@pytest.mark.asyncio
async def test_copy_carries_the_band(client) -> None:
    """The band is part of the block's intent, so it replicates (unlike
    expires_at, which is room/guest-specific)."""
    src = await _make_room(client, "Src")
    dst = await _make_room(client, "Dst")
    _, s = await _add_schedule(client, src, deadband_override=3.5)
    resp = await client.post(
        f"/api/rooms/{src}/schedules/{s['id']}/copy", json={"target_room_ids": [dst]}
    )
    assert resp.status == 200, await resp.text()
    got = await (await client.get(f"/api/rooms/{dst}/schedules")).json()
    assert len(got) == 1
    assert got[0]["deadband_override"] == 3.5


@pytest.mark.asyncio
async def test_copy_of_a_bandless_block_yields_none(client) -> None:
    src = await _make_room(client, "Src")
    dst = await _make_room(client, "Dst")
    _, s = await _add_schedule(client, src)
    resp = await client.post(
        f"/api/rooms/{src}/schedules/{s['id']}/copy", json={"target_room_ids": [dst]}
    )
    assert resp.status == 200, await resp.text()
    got = await (await client.get(f"/api/rooms/{dst}/schedules")).json()
    assert got[0]["deadband_override"] is None


@pytest.mark.asyncio
async def test_copy_carries_a_zero_band(client) -> None:
    """0.0 is falsy — it must replicate as 0.0, not collapse to null."""
    src = await _make_room(client, "Src")
    dst = await _make_room(client, "Dst")
    _, s = await _add_schedule(client, src, deadband_override=0)
    resp = await client.post(
        f"/api/rooms/{src}/schedules/{s['id']}/copy", json={"target_room_ids": [dst]}
    )
    assert resp.status == 200, await resp.text()
    got = await (await client.get(f"/api/rooms/{dst}/schedules")).json()
    assert got[0]["deadband_override"] == 0


@pytest.mark.asyncio
async def test_copy_with_band_still_drops_expiry_and_lands_enabled(client) -> None:
    """Carrying the band must not change the other copy semantics (#359)."""
    src = await _make_room(client, "Src")
    dst = await _make_room(client, "Dst")
    future = (tz.now_local().replace(tzinfo=None) + timedelta(days=3)).isoformat()
    _, s = await _add_schedule(client, src, deadband_override=1.5, expires_at=future)
    resp = await client.post(
        f"/api/rooms/{src}/schedules/{s['id']}/copy", json={"target_room_ids": [dst]}
    )
    assert resp.status == 200, await resp.text()
    got = await (await client.get(f"/api/rooms/{dst}/schedules")).json()
    assert got[0]["deadband_override"] == 1.5
    assert got[0]["expires_at"] is None
    assert got[0]["enabled"] is True


@pytest.mark.asyncio
async def test_copy_carries_the_band_to_every_target_room(client) -> None:
    src = await _make_room(client, "Src")
    a = await _make_room(client, "A")
    b = await _make_room(client, "B")
    _, s = await _add_schedule(client, src, deadband_override=2.0)
    resp = await client.post(
        f"/api/rooms/{src}/schedules/{s['id']}/copy", json={"target_room_ids": [a, b]}
    )
    assert resp.status == 200, await resp.text()
    for rid in (a, b):
        got = await (await client.get(f"/api/rooms/{rid}/schedules")).json()
        assert got[0]["deadband_override"] == 2.0


@pytest.mark.asyncio
async def test_conflicting_copy_still_carries_the_band(client) -> None:
    """A copy demoted to disabled by an overlap keeps its band, so re-enabling
    it needs no re-entry."""
    src = await _make_room(client, "Src")
    dst = await _make_room(client, "Dst")
    _, s = await _add_schedule(client, src, deadband_override=2.0)
    await _add_schedule(client, dst)  # dst already holds the same enabled slot
    resp = await client.post(
        f"/api/rooms/{src}/schedules/{s['id']}/copy", json={"target_room_ids": [dst]}
    )
    assert resp.status == 200, await resp.text()
    result = (await resp.json())[0]
    assert result["status"] == "created_disabled_conflict"
    assert await _band_of(client, dst, result["schedule_id"]) == 2.0


# — the GET/list boundary always echoes raw °F —


@pytest.mark.asyncio
async def test_list_echoes_the_band(client) -> None:
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room, deadband_override=2.5)
    got = await (await client.get(f"/api/rooms/{room}/schedules")).json()
    assert got[0]["deadband_override"] == 2.5
    assert got[0]["id"] == s["id"]


# ── Schedule display name (Issue #520) ──────────────────────────────────────
#
# The name is an optional LABEL on the write boundary — normalized by
# `schedule_rules.normalize_name` (shared with the MCP boundary), nullable, and
# never an identifier: `id` still addresses the block, and renaming never moves
# it. Create uses "absent → None"; update uses a presence sentinel so an omitted
# key preserves the stored name while null or blank clears it.


async def _name_of(client, room_id: str, schedule_id: str) -> str | None:
    got = await (await client.get(f"/api/rooms/{room_id}/schedules")).json()
    block = next(g for g in got if g["id"] == schedule_id)
    name: str | None = block["name"]
    return name


# — create —


@pytest.mark.asyncio
async def test_create_with_name_stores_it(client) -> None:
    room = await _make_room(client, "R")
    status, body = await _add_schedule(client, room, name="Weekday night setback")
    assert status == 201, body
    assert body["name"] == "Weekday night setback"
    assert await _name_of(client, room, body["id"]) == "Weekday night setback"


@pytest.mark.asyncio
async def test_create_with_the_name_key_absent_stores_none(client) -> None:
    """The default is unnamed — a client that has never heard of the field
    keeps working exactly as before #520."""
    room = await _make_room(client, "R")
    status, body = await _add_schedule(client, room)
    assert status == 201, body
    assert body["name"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", [None, "", "   ", "\n"])
async def test_create_with_blank_name_stores_none(client, blank) -> None:
    """Null, empty and whitespace-only all mean unnamed — never a stored ""."""
    room = await _make_room(client, "R")
    status, body = await _add_schedule(client, room, name=blank)
    assert status == 201, body
    assert body["name"] is None


@pytest.mark.asyncio
async def test_create_normalizes_surrounding_and_internal_whitespace(client) -> None:
    room = await _make_room(client, "R")
    status, body = await _add_schedule(client, room, name="  Night   setback \n")
    assert status == 201, body
    assert body["name"] == "Night setback"


@pytest.mark.asyncio
async def test_create_accepts_a_name_at_the_length_limit(client) -> None:
    room = await _make_room(client, "R")
    at_limit = "x" * schedule_rules.MAX_NAME_LENGTH
    status, body = await _add_schedule(client, room, name=at_limit)
    assert status == 201, body
    assert body["name"] == at_limit


@pytest.mark.asyncio
async def test_create_rejects_an_over_long_name(client) -> None:
    room = await _make_room(client, "R")
    status, body = await _add_schedule(
        client, room, name="x" * (schedule_rules.MAX_NAME_LENGTH + 1)
    )
    assert status == 400
    assert "name" in body["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [5, 1.5, True, [], {"a": 1}])
async def test_create_rejects_a_non_string_name(client, name) -> None:
    room = await _make_room(client, "R")
    status, body = await _add_schedule(client, room, name=name)
    assert status == 400
    assert "name must be a string" in body["error"]


@pytest.mark.asyncio
async def test_create_rejecting_the_name_creates_nothing(client) -> None:
    """A rejected name must not leave a half-created block behind — the same
    all-or-nothing guarantee the band (#517) has."""
    room = await _make_room(client, "R")
    status, _ = await _add_schedule(client, room, name=123)
    assert status == 400
    assert await (await client.get(f"/api/rooms/{room}/schedules")).json() == []


# — update —


@pytest.mark.asyncio
async def test_update_sets_the_name(client) -> None:
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room)
    resp = await client.put(
        f"/api/rooms/{room}/schedules/{s['id']}", json={"name": "  Guest stay  "}
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["name"] == "Guest stay"
    assert await _name_of(client, room, s["id"]) == "Guest stay"


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", [None, "", "   "])
async def test_update_with_a_blank_name_clears_it(client, blank) -> None:
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room, name="Guest stay")
    resp = await client.put(f"/api/rooms/{room}/schedules/{s['id']}", json={"name": blank})
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["name"] is None
    assert await _name_of(client, room, s["id"]) is None


@pytest.mark.asyncio
async def test_update_with_the_key_omitted_preserves_the_name(client) -> None:
    """The presence sentinel: editing an unrelated field must not silently
    un-name a block."""
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room, name="Guest stay")
    resp = await client.put(f"/api/rooms/{room}/schedules/{s['id']}", json={"target_temp": 70})
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["name"] == "Guest stay"


@pytest.mark.asyncio
async def test_update_never_changes_the_id(client) -> None:
    """A name is a label, not an identity — whatever addresses the block over
    REST/MCP keeps addressing it after a rename."""
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room)
    resp = await client.put(f"/api/rooms/{room}/schedules/{s['id']}", json={"name": "Renamed"})
    assert (await resp.json())["id"] == s["id"]


@pytest.mark.asyncio
async def test_update_rejects_a_bad_name_and_leaves_the_stored_value(client) -> None:
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room, name="Guest stay")
    resp = await client.put(
        f"/api/rooms/{room}/schedules/{s['id']}",
        json={"name": "x" * (schedule_rules.MAX_NAME_LENGTH + 1)},
    )
    assert resp.status == 400
    assert await _name_of(client, room, s["id"]) == "Guest stay"


@pytest.mark.asyncio
async def test_update_rejecting_the_name_does_not_apply_other_fields(client) -> None:
    """Validation runs before anything is persisted, so a rejected request is a
    no-op on every field it carried."""
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room, name="Guest stay")
    resp = await client.put(
        f"/api/rooms/{room}/schedules/{s['id']}",
        json={"target_temp": 61, "name": 42},
    )
    assert resp.status == 400
    got = await (await client.get(f"/api/rooms/{room}/schedules")).json()
    assert got[0]["target_temp"] == 68
    assert got[0]["name"] == "Guest stay"


@pytest.mark.asyncio
async def test_name_survives_enable_disable_round_trip(client) -> None:
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room, name="Guest stay")
    await client.put(f"/api/rooms/{room}/schedules/{s['id']}", json={"enabled": False})
    await client.put(f"/api/rooms/{room}/schedules/{s['id']}", json={"enabled": True})
    assert await _name_of(client, room, s["id"]) == "Guest stay"


# — copy —


@pytest.mark.asyncio
async def test_copy_carries_the_name(client) -> None:
    """A name describes what the block is FOR, which is exactly what should
    replicate. Names are per-room labels, so the same one in two rooms is the
    intent, not a collision."""
    src, dst = await _make_room(client, "Src"), await _make_room(client, "Dst")
    _, s = await _add_schedule(client, src, name="Night setback")
    resp = await client.post(
        f"/api/rooms/{src}/schedules/{s['id']}/copy", json={"target_room_ids": [dst]}
    )
    assert resp.status == 200, await resp.text()
    result = (await resp.json())[0]
    assert await _name_of(client, dst, result["schedule_id"]) == "Night setback"
    # The copy is a distinct block that happens to share a label.
    assert result["schedule_id"] != s["id"]


@pytest.mark.asyncio
async def test_copy_of_an_unnamed_block_stays_unnamed(client) -> None:
    src, dst = await _make_room(client, "Src"), await _make_room(client, "Dst")
    _, s = await _add_schedule(client, src)
    resp = await client.post(
        f"/api/rooms/{src}/schedules/{s['id']}/copy", json={"target_room_ids": [dst]}
    )
    result = (await resp.json())[0]
    assert await _name_of(client, dst, result["schedule_id"]) is None


# — the GET/list boundary always echoes the name —


@pytest.mark.asyncio
async def test_list_echoes_the_name(client) -> None:
    room = await _make_room(client, "R")
    _, s = await _add_schedule(client, room, name="Night setback")
    got = await (await client.get(f"/api/rooms/{room}/schedules")).json()
    assert got[0]["name"] == "Night setback"
    assert got[0]["id"] == s["id"]
