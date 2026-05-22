"""Characterization tests for mid-cycle trigger changes (Issue #215).

These tests are written **before** the #215 rewrite to pin the current
behaviour of the cycle lifecycle, so the rewrite (debounce + update-in-place
instead of full teardown) cannot silently break adjacent functionality.

Two groups of tests live here:

  * CURRENT-BEHAVIOUR tests — marked clearly in their docstrings. They assert
    today's "trigger change → terminate the whole cycle" behaviour. The #215
    rewrite intentionally changes these; they will be updated in that PR.

  * REGRESSION GUARDS — adjacent cycle-lifecycle behaviour (room add/remove,
    completion, abort, direction-flip filtering, cycle continuity) that must
    survive the rewrite unchanged.

All tests drive the full engine through the aiohttp app against a fake HA.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_heating_thermostat(fake_ha, ambient: float = 65.0) -> None:
    """A thermostat in heat mode, idle, with ambient below the test targets."""
    fake_ha.seed_state(
        THERMO,
        "heat",
        {"current_temperature": ambient, "temperature": ambient, "hvac_action": "idle"},
    )


async def _all_day_schedule(client, room_id: str, target: float) -> str:
    """Attach a schedule covering 'now' and return its id."""
    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).time().replace(second=0, microsecond=0)
    end = (now + timedelta(hours=1)).time().replace(second=0, microsecond=0)
    resp = await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={
            "days_of_week": list(range(7)),
            "start_time": start.isoformat(timespec="minutes"),
            "end_time": end.isoformat(timespec="minutes"),
            "target_temp": target,
        },
    )
    return (await resp.json())["id"]


async def _heating_room(
    client,
    fake_ha,
    name: str,
    sensor: str,
    vent: str,
    target: float,
    room_temp: float = 65.0,
    schedule: bool = True,
) -> tuple[str, str | None]:
    """Create a room (sensor + vent) wired to THERMO; optionally schedule it.

    Returns (room_id, schedule_id). schedule_id is None when schedule=False.
    """
    fake_ha.seed_state(sensor, str(room_temp), {"unit_of_measurement": "°F"})
    fake_ha.seed_state(vent, "closed", {})
    resp = await client.post("/api/rooms", json={"name": name, "thermostat_entity_id": THERMO})
    room_id = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": sensor})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": vent, "control_method": "open_close"},
    )
    sched_id = await _all_day_schedule(client, room_id, target) if schedule else None
    return room_id, sched_id


async def _logs(client) -> list[dict]:
    return await (await client.get("/api/logs")).json()


async def _open_cycles(client) -> list[dict]:
    return [c for c in await _logs(client) if c["ended_at"] is None]


async def _warning_messages(client) -> list[str]:
    events = await (await client.get("/api/logs/events?level=warning")).json()
    return [e["message"] for e in events]


def _engine_state(client) -> str:
    return client.app["scheduler"]._engines[THERMO].cycle_state.value


# ===========================================================================
# REGRESSION GUARDS — must survive the #215 rewrite unchanged
# ===========================================================================


@pytest.mark.asyncio
async def test_stable_trigger_keeps_single_cycle_running(client, fake_ha, tick) -> None:
    """A trigger that does not change across ticks keeps exactly one cycle
    running — no teardown, no second cycle log."""
    _seed_heating_thermostat(fake_ha)
    await _heating_room(client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", 68.0)

    await tick()
    first = await _open_cycles(client)
    assert len(first) == 1
    cycle_id = first[0]["id"]

    # Three more ticks with nothing changing.
    for _ in range(3):
        await tick()

    logs = await _logs(client)
    assert len(logs) == 1, "no extra cycle logs should appear"
    assert logs[0]["id"] == cycle_id
    assert logs[0]["ended_at"] is None, "the cycle must still be running"
    assert _engine_state(client) == "running"


@pytest.mark.asyncio
async def test_room_added_mid_cycle_continues_without_teardown(client, fake_ha, tick) -> None:
    """A second room becoming active mid-cycle is added to the SAME cycle —
    the cycle log is not closed and no new one is opened."""
    _seed_heating_thermostat(fake_ha)
    await _heating_room(client, fake_ha, "RoomA", "sensor.a", "cover.a", 68.0)
    # RoomB exists but has no schedule yet — not active.
    room_b, _ = await _heating_room(
        client, fake_ha, "RoomB", "sensor.b", "cover.b", 68.0, schedule=False
    )

    await tick()
    started = await _open_cycles(client)
    assert len(started) == 1
    cycle_id = started[0]["id"]

    # RoomB gets a schedule → it becomes active on the next tick.
    await _all_day_schedule(client, room_b, 68.0)
    await tick()

    logs = await _logs(client)
    assert len(logs) == 1, "adding a room must not open a second cycle"
    assert logs[0]["id"] == cycle_id, "the running cycle must be the same one"
    assert logs[0]["ended_at"] is None
    assert _engine_state(client) == "running"


@pytest.mark.asyncio
async def test_room_removed_mid_cycle_continues_without_teardown(client, fake_ha, tick) -> None:
    """When one of several rooms becomes idle, it is removed from the cycle and
    the cycle continues for the rest — no teardown."""
    _seed_heating_thermostat(fake_ha)
    await _heating_room(client, fake_ha, "RoomA", "sensor.a", "cover.a", 68.0)
    room_b, sched_b = await _heating_room(client, fake_ha, "RoomB", "sensor.b", "cover.b", 68.0)

    await tick()
    started = await _open_cycles(client)
    assert len(started) == 1
    cycle_id = started[0]["id"]

    # RoomB's schedule is deleted → RoomB goes idle, RoomA keeps heating.
    await client.delete(f"/api/rooms/{room_b}/schedules/{sched_b}")
    await tick()

    logs = await _logs(client)
    assert len(logs) == 1, "removing a room must not open a second cycle"
    assert logs[0]["id"] == cycle_id
    assert logs[0]["ended_at"] is None, "cycle continues for the remaining room"
    assert _engine_state(client) == "running"


@pytest.mark.asyncio
async def test_all_rooms_idle_aborts_the_cycle(client, fake_ha, tick) -> None:
    """When every room goes idle the cycle is aborted (not left running)."""
    _seed_heating_thermostat(fake_ha)
    room_id, sched_id = await _heating_room(
        client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", 68.0
    )

    await tick()
    assert len(await _open_cycles(client)) == 1

    await client.delete(f"/api/rooms/{room_id}/schedules/{sched_id}")
    await tick()

    assert await _open_cycles(client) == [], "cycle must be closed once no rooms are active"
    assert _engine_state(client) == "idle"


@pytest.mark.asyncio
async def test_room_reaching_target_completes_the_cycle(client, fake_ha, tick) -> None:
    """A room that reaches its target completes the cycle cleanly."""
    _seed_heating_thermostat(fake_ha)
    await _heating_room(client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", 68.0)

    await tick()
    assert len(await _open_cycles(client)) == 1

    # Room reaches its 68°F target (within deadband — not so far past it that
    # the mode filter would treat it as needing cooling).
    await fake_ha.set_entity_state("sensor.bed", "68.0", {"unit_of_measurement": "°F"})
    await tick()

    open_cycles = await _open_cycles(client)
    assert open_cycles == [], "cycle should complete once the room is at target"
    closed = (await _logs(client))[0]
    assert closed["ended_reason"] == "completed"


@pytest.mark.asyncio
async def test_direction_flip_is_handled_by_the_mode_filter(client, fake_ha, tick) -> None:
    """A genuine direction flip (room now needs the opposite of the locked
    cycle mode) is removed by _filter_rooms_for_mode — it never reaches the
    trigger-change path. This invariant is what lets the #215 rewrite treat
    every surviving trigger change as same-direction.

    Setup: a heating cycle, then the schedule target is dropped well below
    the room temperature so the room now needs cooling.
    """
    _seed_heating_thermostat(fake_ha)
    room_id, sched_id = await _heating_room(
        client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", 68.0, room_temp=66.0
    )

    await tick()
    assert len(await _open_cycles(client)) == 1

    # Drop the target to 60°F — the room at 66°F now needs cooling, not heating.
    await client.put(
        f"/api/rooms/{room_id}/schedules/{sched_id}",
        json={
            "days_of_week": list(range(7)),
            "start_time": "00:00",
            "end_time": "23:59",
            "target_temp": 60.0,
        },
    )
    await tick()

    closed = (await _logs(client))[0]
    assert closed["ended_at"] is not None
    # The reason is the mode-filter abort, NOT a "trigger changed" teardown.
    assert "filtering" in (closed["ended_reason"] or "")
    assert "trigger changed" not in (closed["ended_reason"] or "")


# ===========================================================================
# CURRENT-BEHAVIOUR tests — the #215 rewrite intentionally changes these
# ===========================================================================


@pytest.mark.asyncio
async def test_CURRENT_target_change_same_direction_terminates(client, fake_ha, tick) -> None:
    """CURRENT BEHAVIOUR (#215 will change this).

    A persisting room whose schedule target moves 68°F → 72°F — still a
    heating demand (ambient 65°F) — currently tears the whole cycle down with
    ended_reason 'trigger changed'. After #215 this becomes an update in place.
    """
    _seed_heating_thermostat(fake_ha)
    room_id, sched_id = await _heating_room(
        client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", 68.0
    )

    await tick()
    started = await _open_cycles(client)
    assert len(started) == 1
    original_cycle_id = started[0]["id"]

    # Bump the schedule target up — still a heating demand.
    await client.put(
        f"/api/rooms/{room_id}/schedules/{sched_id}",
        json={
            "days_of_week": list(range(7)),
            "start_time": "00:00",
            "end_time": "23:59",
            "target_temp": 72.0,
        },
    )
    await tick()

    closed = next(c for c in await _logs(client) if c["id"] == original_cycle_id)
    assert closed["ended_at"] is not None, "current behaviour: the cycle is torn down"
    assert "trigger changed" in (closed["ended_reason"] or "")


@pytest.mark.asyncio
async def test_CURRENT_source_change_terminates(client, fake_ha, tick) -> None:
    """CURRENT BEHAVIOUR (#215 will change this).

    A presence cycle handed over to a schedule at the SAME target (70°F)
    currently terminates because the source string differs. After #215 this
    becomes a metadata-only update in place.
    """
    _seed_heating_thermostat(fake_ha)
    fake_ha.seed_state("sensor.bed", "65.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.bed", "closed", {})
    fake_ha.seed_state("binary_sensor.bed_presence", "off", {})

    resp = await client.post(
        "/api/rooms",
        json={
            "name": "Bedroom",
            "thermostat_entity_id": THERMO,
            "system_wide_temp": 70.0,
            "presence_holdover_hours": 2.0,
        },
    )
    room_id = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.bed"})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": "cover.bed", "control_method": "open_close"},
    )
    await client.post(
        f"/api/rooms/{room_id}/presence",
        json={"entity_id": "binary_sensor.bed_presence"},
    )

    await fake_ha.set_entity_state("binary_sensor.bed_presence", "on", {})
    await tick()
    started = await _open_cycles(client)
    assert len(started) == 1
    presence_cycle_id = started[0]["id"]
    assert next(iter(started[0]["rooms"].values()))["source"] == "presence"

    # Schedule at the SAME 70°F target — only the source string changes.
    await _all_day_schedule(client, room_id, 70.0)
    await tick()

    closed = next(c for c in await _logs(client) if c["id"] == presence_cycle_id)
    assert closed["ended_at"] is not None, "current behaviour: the cycle is torn down"
    assert "trigger changed" in (closed["ended_reason"] or "")


@pytest.mark.asyncio
async def test_CURRENT_multi_room_one_trigger_change_tears_down_whole_cycle(
    client, fake_ha, tick
) -> None:
    """CURRENT BEHAVIOUR (#215 will change this).

    Two rooms heating in one cycle. Changing ONE room's target currently
    tears down the cycle for BOTH rooms. After #215 the cycle should update in
    place, leaving the unchanged room undisturbed.
    """
    _seed_heating_thermostat(fake_ha)
    await _heating_room(client, fake_ha, "RoomA", "sensor.a", "cover.a", 68.0)
    room_b, sched_b = await _heating_room(client, fake_ha, "RoomB", "sensor.b", "cover.b", 68.0)

    await tick()
    started = await _open_cycles(client)
    assert len(started) == 1
    cycle_id = started[0]["id"]

    # Change only RoomB's target — still heating (ambient 65°F).
    await client.put(
        f"/api/rooms/{room_b}/schedules/{sched_b}",
        json={
            "days_of_week": list(range(7)),
            "start_time": "00:00",
            "end_time": "23:59",
            "target_temp": 71.0,
        },
    )
    await tick()

    closed = next(c for c in await _logs(client) if c["id"] == cycle_id)
    assert closed["ended_at"] is not None, (
        "current behaviour: a single room's change tears down the whole cycle"
    )
    assert "trigger changed" in (closed["ended_reason"] or "")


@pytest.mark.asyncio
async def test_CURRENT_trigger_change_teardown_then_offtime_lockout(client, fake_ha, tick) -> None:
    """CURRENT BEHAVIOUR (#215 will change this).

    With short-cycle protection enabled (#208), a 'trigger changed' teardown
    is immediately followed by the compressor off-time lockout blocking the
    restart — so a room that obviously still needs heat gets none until the
    lockout expires. This is the exact pathology #215 exists to remove: after
    the rewrite there is no teardown, so no lockout, and the room keeps
    heating.
    """
    _seed_heating_thermostat(fake_ha)
    room_id, sched_id = await _heating_room(
        client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", 68.0
    )
    # Enable the compressor off-time lockout.
    await client.put(f"/api/thermostats/{THERMO}", json={"min_cycle_offtime_min": 10})

    await tick()
    assert len(await _open_cycles(client)) == 1

    # Change the target — triggers the teardown.
    await client.put(
        f"/api/rooms/{room_id}/schedules/{sched_id}",
        json={
            "days_of_week": list(range(7)),
            "start_time": "00:00",
            "end_time": "23:59",
            "target_temp": 72.0,
        },
    )
    await tick()
    assert await _open_cycles(client) == [], "current behaviour: cycle torn down"

    # Next tick: the engine wants to restart but the off-time lockout blocks it.
    await tick()
    assert await _open_cycles(client) == [], "off-time lockout blocks the restart"
    warnings = await _warning_messages(client)
    assert any("off-time lockout" in m.lower() for m in warnings), warnings
