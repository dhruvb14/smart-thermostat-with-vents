"""Cycle-join atomicity: a locked DB must not strand ventless rooms (#615).

#603 made ``_repair_missing_room_state`` atomic so a failed repair retries.
This is the sibling case one level up, in ``_start_or_update_cycle``.

``_repair_missing_room_state`` is reachable only through ``_monitor_rooms``'
``rcs is None`` gate, so an entry in ``_room_cycle_states`` is exactly what
makes a half-joined room *invisible* to the repair. Both join paths used to
publish into that map **before** their upsert, and on a fresh start the vents
were opened only by a tail loop *after* the per-room loop — so one room's
``OperationalError("database is locked")`` (#286) left every room in the zone
in the map, none of them vented, and none of them repairable, until
``cycle_timeout_hours`` (3 h by default).

These tests pin the post-fix contract on both join paths: a room is published
into ``_room_cycle_states`` only once its row is persisted **and** its vents
are open, one room's failure never abandons its siblings, and the failed room
is left missing from the map so the #427/#603 repair heals it next tick.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from backend import db
from backend.tests.integration.fake_ha import ServiceCall

THERMO = "climate.test_thermostat"
A_SENSOR = "sensor.room_a"
A_VENT = "cover.room_a_vent"
B_SENSOR = "sensor.room_b"
B_VENT = "cover.room_b_vent"


def _engine(client):
    return client.app["scheduler"]._engines[THERMO]


async def _make_room(client, name: str, sensor: str, vent: str, target: float) -> str:
    resp = await client.post("/api/rooms", json={"name": name, "thermostat_entity_id": THERMO})
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": sensor})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": vent, "control_method": "open_close"},
    )
    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).time().replace(second=0, microsecond=0)
    end = (now + timedelta(hours=1)).time().replace(second=0, microsecond=0)
    await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={
            "days_of_week": list(range(7)),
            "start_time": start.isoformat(timespec="minutes"),
            "end_time": end.isoformat(timespec="minutes"),
            "target_temp": target,
        },
    )
    return room_id


def _fail_upserts_for(monkeypatch, room_ids: set[str]) -> None:
    """Make ``db.upsert_room_cycle_state`` raise for the given rooms only.

    Mutating ``room_ids`` in place disarms the fault for later ticks, which is
    how each test lets the repair path succeed on the tick after the failure.
    """
    real = db.upsert_room_cycle_state

    async def flaky(conn, rcs):
        if rcs.room_id in room_ids:
            raise RuntimeError("database is locked")
        return await real(conn, rcs)

    monkeypatch.setattr(db, "upsert_room_cycle_state", flaky)


async def _persisted_room_ids(client, cycle_id: str) -> set[str]:
    conn = client.app["scheduler"]._db_conn
    rows = await db.get_room_cycle_states(conn, cycle_id)
    return {r.room_id for r in rows}


def _opened(fake_ha, entity_id: str) -> bool:
    return any(c.data.get("entity_id") == entity_id for c in fake_ha.calls_for("open_cover"))


@pytest.mark.asyncio
async def test_fresh_start_upsert_failure_leaves_sibling_vented_and_room_repairable(
    client, fake_ha, tick, monkeypatch
) -> None:
    """Acceptance criterion 1 — the fresh-start path (was ~995-997)."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 80.0, "temperature": 78.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state(A_SENSOR, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(B_SENSOR, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(A_VENT, "closed", {})
    fake_ha.seed_state(B_VENT, "closed", {})
    room_a = await _make_room(client, "RoomA", A_SENSOR, A_VENT, 72.0)
    room_b = await _make_room(client, "RoomB", B_SENSOR, B_VENT, 72.0)

    doomed = {room_b}
    _fail_upserts_for(monkeypatch, doomed)

    fake_ha.reset_calls()
    await tick()  # fresh cooling start; RoomB's upsert raises

    eng = _engine(client)
    assert eng.cycle_state.value == "running"
    assert set(eng._active_rooms) == {room_a, room_b}
    # The rest of the cycle setup still ran: before this fix the raise
    # propagated out of _start_or_update_cycle, so the tail of the method —
    # the idle-vent close and the only _set_thermostat_setpoint call site in
    # the engine — was skipped for the whole tick.
    assert fake_ha.calls_for("set_temperature"), (
        "one room's DB failure must not abandon the rest of the cycle setup"
    )
    # RoomA fully joined: persisted, vented, published.
    assert room_a in eng._room_cycle_states
    assert _opened(fake_ha, A_VENT), "a sibling's DB failure must not leave RoomA ventless"
    # RoomB's join failed as a unit — no map entry, so the repair can see it.
    assert room_b not in eng._room_cycle_states
    cycle_id = eng._cycle_log.id
    assert await _persisted_room_ids(client, cycle_id) == {room_a}

    # Next tick: the #427/#603 repair heals RoomB.
    doomed.clear()
    fake_ha.reset_calls()
    await tick()
    assert room_b in eng._room_cycle_states, "the half-joined room must be repaired"
    assert _opened(fake_ha, B_VENT), "the repaired room's vents must be opened"
    assert await _persisted_room_ids(client, cycle_id) == {room_a, room_b}


@pytest.mark.asyncio
async def test_mid_cycle_join_upsert_failure_leaves_room_repairable(
    client, fake_ha, tick, monkeypatch
) -> None:
    """Acceptance criterion 2 — the mid-cycle-join path (was ~1048-1050)."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 80.0, "temperature": 78.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state(A_SENSOR, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(A_VENT, "closed", {})
    room_a = await _make_room(client, "RoomA", A_SENSOR, A_VENT, 72.0)

    await tick()  # cycle starts with RoomA alone
    eng = _engine(client)
    assert eng.cycle_state.value == "running"
    cycle_id = eng._cycle_log.id

    # RoomB appears mid-cycle and lands in `added`.
    fake_ha.seed_state(B_SENSOR, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(B_VENT, "closed", {})
    room_b = await _make_room(client, "RoomB", B_SENSOR, B_VENT, 72.0)

    doomed = {room_b}
    _fail_upserts_for(monkeypatch, doomed)
    fake_ha.reset_calls()
    await tick()

    assert set(eng._active_rooms) == {room_a, room_b}
    assert room_b not in eng._room_cycle_states
    assert not _opened(fake_ha, B_VENT), "a room whose row was not written must not be vented"
    assert await _persisted_room_ids(client, cycle_id) == {room_a}
    # RoomA keeps its join, and the work after the `added` loop still ran —
    # before this fix the raise propagated and the cycle log's room snapshot
    # was never merged.
    assert room_a in eng._room_cycle_states
    assert room_b in json.loads(eng._cycle_log.rooms_json)

    doomed.clear()
    fake_ha.reset_calls()
    await tick()
    assert room_b in eng._room_cycle_states
    assert _opened(fake_ha, B_VENT)
    assert await _persisted_room_ids(client, cycle_id) == {room_a, room_b}


@pytest.mark.asyncio
async def test_a_fresh_start_opens_each_vent_exactly_once(client, fake_ha, tick, monkeypatch):
    """The tail vent loop must skip rooms this same call already joined.

    Moving the vent open *into* the per-room join (so a published room is
    always a vented room) put it before the tail "open all active room vents"
    loop that used to be the only opener. Nothing dedupes the two against a
    real HA: that loop's own gate is ``vent_closed_at is None``, true for a
    just-joined room, and ``open_room_vents``' fully-open skip reads
    ``HAClient``'s state cache, which is refreshed only by a ``state_changed``
    websocket push — one that has not arrived within the same tick that
    issued the ``open_cover``.

    ``FakeHomeAssistant.open_cover`` writes ``state: "open"`` synchronously, so
    against the unmodified double the fully-open skip *does* absorb the second
    call and this test cannot fail (verified by removing the guard). The
    monkeypatch below restores the real client's behaviour — record the call,
    leave the cached state alone — which is the only condition under which the
    duplicate is observable, and therefore the only honest way to pin it.
    """
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 80.0, "temperature": 78.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state(A_SENSOR, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(B_SENSOR, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(A_VENT, "closed", {})
    fake_ha.seed_state(B_VENT, "closed", {})
    await _make_room(client, "RoomA", A_SENSOR, A_VENT, 72.0)
    await _make_room(client, "RoomB", B_SENSOR, B_VENT, 72.0)

    async def lagging_open(entity_id: str) -> None:
        # Real HA: the service call is recorded, the cached state catches up
        # later via a websocket push that this tick will not see.
        fake_ha.calls.append(
            ServiceCall(domain="cover", service="open_cover", data={"entity_id": entity_id})
        )

    monkeypatch.setattr(fake_ha, "open_cover", lagging_open)

    fake_ha.reset_calls()
    await tick()

    assert _engine(client).cycle_state.value == "running"
    for vent in (A_VENT, B_VENT):
        opens = [c for c in fake_ha.calls_for("open_cover") if c.data.get("entity_id") == vent]
        assert len(opens) == 1, f"{vent} was opened {len(opens)}x on one fresh start, expected 1"
