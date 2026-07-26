"""Mid-cycle trigger-change behaviour (Issue #215).

A persisting room's trigger — its ``source`` (override / schedule / presence)
and ``target_temp`` — can change while a cycle runs. The old engine tore the
whole cycle down on the first differing tick, which stopped the HVAC and,
combined with the #208 compressor off-time lockout, could lock a room out of
heat it obviously still needed. #215 replaces that teardown with an in-place
update of the running cycle.

Three groups of tests live here:

  * REGRESSION GUARDS — adjacent cycle-lifecycle behaviour (room add/remove,
    completion, abort, direction-flip filtering, cycle continuity) that the
    #215 rewrite must NOT change. Written before the rewrite to pin it.

  * UPDATE-IN-PLACE tests — the #215 behaviour itself: a same-direction
    target change, a source-only handoff, a multi-room cycle where one room
    changes, and the teardown-then-lockout pathology that the rewrite removes.

  * PER-SCHEDULE DEADBAND tests (#517) — the block's ``deadband_override`` is
    the third component of the trigger, alongside source and target. It must
    fire the in-place update when it moves, and must not churn when it does
    not (the #408 failure mode).

A genuine direction flip never reaches the trigger-change path —
``_filter_rooms_for_mode`` drops any room that now needs the opposite of the
locked cycle mode first — so every surviving trigger change is same-direction
and can always be applied in place.

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
    sched_id: str = (await resp.json())["id"]
    return sched_id


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
    logs: list[dict] = await (await client.get("/api/logs")).json()
    return logs


async def _open_cycles(client) -> list[dict]:
    return [c for c in await _logs(client) if c["ended_at"] is None]


async def _warning_messages(client) -> list[str]:
    events = await (await client.get("/api/logs/events?level=warning")).json()
    return [e["message"] for e in events]


def _engine_state(client) -> str:
    return str(client.app["scheduler"]._engines[THERMO].cycle_state.value)


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

    # The removed room must still appear in the cycle-detail view with its
    # name — its RoomCycleState row belongs to this cycle, and the detail
    # endpoint looks up name/source from rooms_json. The mid-cycle snapshot
    # refresh merges rather than rebuilds, so RoomB's entry is preserved.
    detail = await (await client.get(f"/api/logs/{cycle_id}/detail")).json()
    detail_names = {r["room_id"]: r["name"] for r in detail["rooms"]}
    assert detail_names.get(room_b) == "RoomB", (
        f"removed room must keep its name in the cycle detail, got {detail_names}"
    )


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
    # SAFETY PROPERTY (what actually matters): a direction flip must STOP the
    # cycle — the engine must not keep heating a room that now needs cooling.
    assert _engine_state(client) == "idle", "the heating cycle must not continue"
    assert await _open_cycles(client) == [], "no cycle may remain open after the flip"
    # IMPLEMENTATION DETAIL (characterization): today that stop comes from the
    # mode filter, not a "trigger changed" teardown. Kept as a regression note
    # for the #215 path; if a refactor relocates the stop, update this pair but
    # keep the safety assertions above.
    assert "filtering" in (closed["ended_reason"] or "")
    assert "trigger changed" not in (closed["ended_reason"] or "")


# ===========================================================================
# UPDATE-IN-PLACE behaviour — the #215 rewrite. A mid-cycle trigger change on
# a persisting room is applied to the running cycle instead of tearing it down.
# ===========================================================================


@pytest.mark.asyncio
async def test_target_change_same_direction_updates_in_place(client, fake_ha, tick) -> None:
    """A persisting room whose schedule target moves 68°F → 72°F — still a
    heating demand (ambient 65°F) — is updated in place: the SAME cycle keeps
    running, the cycle log reflects the new target, and the setpoint is
    re-derived (72 + 2°F overshoot = 74°F)."""
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

    open_cycles = await _open_cycles(client)
    assert len(open_cycles) == 1, "the cycle must keep running — no teardown"
    assert open_cycles[0]["id"] == original_cycle_id, "it must be the SAME cycle"
    assert len(await _logs(client)) == 1, "no second cycle log was created"

    # The cycle log's room snapshot reflects the new target.
    assert next(iter(open_cycles[0]["rooms"].values()))["target"] == 72.0

    # The setpoint was re-derived for the new target (72 + 2°F overshoot).
    setpoint_calls = fake_ha.calls_for("set_temperature")
    assert setpoint_calls, "engine should have written a setpoint"
    assert setpoint_calls[-1].data["temperature"] == pytest.approx(74.0, abs=0.5)

    # The cycle-detail view surfaces the in-place update: its setpoint history
    # carries a 'trigger updated in place' entry the UI renders verbatim.
    detail = await (await client.get(f"/api/logs/{original_cycle_id}/detail")).json()
    reasons = [sp["reason"] for sp in detail["setpoint_history"]]
    assert "trigger updated in place" in reasons, reasons


@pytest.mark.asyncio
async def test_source_change_updates_in_place(client, fake_ha, tick) -> None:
    """A presence cycle handed over to a schedule at the SAME target (70°F)
    updates in place: the cycle keeps running and the log's source flips from
    'presence' to 'schedule' without a teardown."""
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

    open_cycles = await _open_cycles(client)
    assert len(open_cycles) == 1, "the cycle must keep running — no teardown"
    assert open_cycles[0]["id"] == presence_cycle_id, "it must be the SAME cycle"
    assert len(await _logs(client)) == 1, "no second cycle log was created"
    # The handoff is reflected in the cycle log: source now 'schedule'.
    room_entry = next(iter(open_cycles[0]["rooms"].values()))
    assert room_entry["source"] == "schedule"
    assert room_entry["target"] == 70.0


@pytest.mark.asyncio
async def test_schedule_to_presence_handoff_updates_in_place(client, fake_ha, tick) -> None:
    """When a schedule ends but a presence holdover is active at the SAME
    target, the cycle continues with source schedule→presence — no teardown
    (Issue #270 handoff matrix; the inverse of the presence→schedule test)."""
    _seed_heating_thermostat(fake_ha)
    fake_ha.seed_state("sensor.bed", "65.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.bed", "closed", {})
    fake_ha.seed_state("binary_sensor.bed_presence", "off", {})

    # Room wired for BOTH presence (system_wide_temp 68) and a schedule at the
    # same 68°F. Schedule outranks presence while both are active.
    resp = await client.post(
        "/api/rooms",
        json={
            "name": "Bedroom",
            "thermostat_entity_id": THERMO,
            "system_wide_temp": 68.0,
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
    sched_id = await _all_day_schedule(client, room_id, 68.0)

    # Fire presence so a holdover exists, then tick — the schedule wins.
    await fake_ha.set_entity_state("binary_sensor.bed_presence", "on", {})
    await tick()
    started = await _open_cycles(client)
    assert len(started) == 1
    cycle_id = started[0]["id"]
    assert next(iter(started[0]["rooms"].values()))["source"] == "schedule"

    # Delete the schedule → the presence holdover (same 68°F) takes over.
    await client.delete(f"/api/rooms/{room_id}/schedules/{sched_id}")
    await tick()

    open_cycles = await _open_cycles(client)
    assert len(open_cycles) == 1, "the cycle must keep running — no teardown"
    assert open_cycles[0]["id"] == cycle_id, "it must be the SAME cycle"
    assert len(await _logs(client)) == 1, "no second cycle log was created"
    room_entry = next(iter(open_cycles[0]["rooms"].values()))
    assert room_entry["source"] == "presence"
    assert room_entry["target"] == 68.0


@pytest.mark.asyncio
async def test_override_to_schedule_handoff_updates_in_place(client, fake_ha, tick) -> None:
    """An override handed back to the underlying schedule at the SAME target
    updates the running cycle in place — source flips override→schedule with no
    teardown (Issue #270 handoff matrix)."""
    _seed_heating_thermostat(fake_ha)
    room_id, _sched = await _heating_room(
        client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", 68.0
    )
    # Override at the same 68°F target → still heating, source 'override'.
    await client.post(
        f"/api/rooms/{room_id}/override", json={"target_temp": 68.0, "duration_hours": 2}
    )

    await tick()
    started = await _open_cycles(client)
    assert len(started) == 1
    cycle_id = started[0]["id"]
    assert next(iter(started[0]["rooms"].values()))["source"] == "override"

    # Clear the override → the schedule (same 68°F) takes back over.
    await client.delete(f"/api/rooms/{room_id}/override")
    await tick()

    open_cycles = await _open_cycles(client)
    assert len(open_cycles) == 1, "the cycle must keep running — no teardown"
    assert open_cycles[0]["id"] == cycle_id, "it must be the SAME cycle"
    assert len(await _logs(client)) == 1, "no second cycle log was created"
    room_entry = next(iter(open_cycles[0]["rooms"].values()))
    assert room_entry["source"] == "schedule"
    assert room_entry["target"] == 68.0


@pytest.mark.asyncio
async def test_multi_room_one_trigger_change_leaves_other_room_undisturbed(
    client, fake_ha, tick
) -> None:
    """Two rooms heating in one cycle. Changing ONE room's target updates the
    cycle in place — the cycle keeps running and the unchanged room stays in
    it at its original target."""
    _seed_heating_thermostat(fake_ha)
    room_a, _ = await _heating_room(client, fake_ha, "RoomA", "sensor.a", "cover.a", 68.0)
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

    open_cycles = await _open_cycles(client)
    assert len(open_cycles) == 1, "the cycle must keep running for both rooms"
    assert open_cycles[0]["id"] == cycle_id, "it must be the SAME cycle"
    assert len(await _logs(client)) == 1, "no second cycle log was created"

    rooms = open_cycles[0]["rooms"]
    assert rooms[room_a]["target"] == 68.0, "RoomA's target is undisturbed"
    assert rooms[room_b]["target"] == 71.0, "RoomB's target was updated in place"

    # Setpoint reflects the most-demanding room (71 + 2°F overshoot).
    setpoint_calls = fake_ha.calls_for("set_temperature")
    assert setpoint_calls[-1].data["temperature"] == pytest.approx(73.0, abs=0.5)


@pytest.mark.asyncio
async def test_trigger_change_does_not_trip_the_offtime_lockout(client, fake_ha, tick) -> None:
    """The pathology #215 exists to remove: with short-cycle protection
    enabled (#208), the old teardown was immediately followed by the
    compressor off-time lockout blocking the restart, leaving a room with no
    heat. With update-in-place there is no teardown, so no lockout — the room
    keeps heating straight through the target change.
    """
    _seed_heating_thermostat(fake_ha)
    room_id, sched_id = await _heating_room(
        client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", 68.0
    )
    # Enable the compressor off-time lockout.
    await client.put(f"/api/thermostats/{THERMO}", json={"min_cycle_offtime_min": 10})

    await tick()
    started = await _open_cycles(client)
    assert len(started) == 1
    cycle_id = started[0]["id"]

    # Change the target — would previously have triggered a teardown.
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
    await tick()

    open_cycles = await _open_cycles(client)
    assert len(open_cycles) == 1, "the cycle keeps running — no teardown"
    assert open_cycles[0]["id"] == cycle_id, "still the same cycle"

    # No teardown means the off-time lockout is never engaged.
    warnings = await _warning_messages(client)
    assert not any("off-time lockout" in m.lower() for m in warnings), warnings


# ===========================================================================
# PER-SCHEDULE DEADBAND (Issue #517) — the band is part of the trigger
#
# `trigger_changed` compares source, requested target AND the schedule's
# deadband_override. The band matters because the monitor paths
# (_monitor_rooms' served-room reopen check, _rooms_drifted_past_deadband) read
# it off self._active_rooms, which is ONLY reassigned by
# _start_or_update_cycle. Without the band in the comparison, crossing from one
# block to another with the same source and target but a different band would
# leave the running cycle monitoring on the stale band until it ended.
#
# The mirror-image risk is the #408 churn: comparing something that is
# recomputed every tick makes _start_or_update_cycle re-run 60 times an hour.
# So both directions are pinned — it must fire when the band moves, and must
# stay quiet when it does not.
# ===========================================================================


def _window() -> tuple[str, str]:
    """A single all-day-ish window shared by several blocks, so two blocks can
    differ ONLY in their band."""
    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).time().replace(second=0, microsecond=0)
    end = (now + timedelta(hours=1)).time().replace(second=0, microsecond=0)
    return start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")


async def _banded_block(
    client,
    room_id: str,
    *,
    target: float,
    band: float | None,
    window: tuple[str, str],
    enabled: bool = True,
) -> str:
    start, end = window
    resp = await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={
            "days_of_week": list(range(7)),
            "start_time": start,
            "end_time": end,
            "target_temp": target,
            "enabled": enabled,
            "deadband_override": band,
        },
    )
    assert resp.status == 201, await resp.text()
    sid: str = (await resp.json())["id"]
    return sid


def _spy_on_cycle_updates(client) -> list[str]:
    """Record every ``_start_or_update_cycle`` call on the running engine.

    That call is the only observable effect of ``trigger_changed`` — it is what
    reassigns ``self._active_rooms`` (and therefore the band every monitor path
    reads). Counting it distinguishes "the band change was noticed" from "the
    engine churns every tick".
    """
    engine = client.app["scheduler"]._engines[THERMO]
    calls: list[str] = []
    original = engine._start_or_update_cycle

    async def _recording(*args, **kwargs):
        calls.append("start_or_update")
        return await original(*args, **kwargs)

    engine._start_or_update_cycle = _recording
    return calls


def _active_band(client, room_id: str) -> float | None:
    engine = client.app["scheduler"]._engines[THERMO]
    band: float | None = engine._active_rooms[room_id].deadband_override
    return band


@pytest.mark.asyncio
async def test_crossing_to_a_block_with_a_different_band_updates_in_place(
    client, fake_ha, tick
) -> None:
    """Two blocks, identical days/times/target, DIFFERENT bands. Swapping which
    one is enabled changes neither the source nor the target — only the band —
    and that alone must re-run _start_or_update_cycle so the running cycle
    monitors on the new band. The cycle itself keeps running (no teardown)."""
    _seed_heating_thermostat(fake_ha)
    fake_ha.seed_state("sensor.bed", "65.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.bed", "closed", {})
    resp = await client.post("/api/rooms", json={"name": "Bedroom", "thermostat_entity_id": THERMO})
    room_id = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.bed"})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": "cover.bed", "control_method": "open_close"},
    )
    window = _window()
    block_a = await _banded_block(client, room_id, target=68.0, band=1.0, window=window)
    # B is parked, so it may share A's slot (#359) — it becomes the match the
    # moment A is disabled.
    block_b = await _banded_block(
        client, room_id, target=68.0, band=2.0, window=window, enabled=False
    )

    await tick()
    started = await _open_cycles(client)
    assert len(started) == 1
    cycle_id = started[0]["id"]
    assert _active_band(client, room_id) == 1.0

    calls = _spy_on_cycle_updates(client)

    # Cross from A to B: same source ('schedule'), same 68°F target, new band.
    await client.put(f"/api/rooms/{room_id}/schedules/{block_a}", json={"enabled": False})
    await client.put(f"/api/rooms/{room_id}/schedules/{block_b}", json={"enabled": True})
    await tick()

    assert len(calls) == 1, "the band change must re-run _start_or_update_cycle"
    assert _active_band(client, room_id) == 2.0, "the running cycle must adopt the new band"

    open_cycles = await _open_cycles(client)
    assert len(open_cycles) == 1, "the cycle must keep running — no teardown"
    assert open_cycles[0]["id"] == cycle_id, "it must be the SAME cycle"
    assert len(await _logs(client)) == 1, "no second cycle log was created"
    assert next(iter(open_cycles[0]["rooms"].values()))["target"] == 68.0, "target is unchanged"

    # #408 guard: once adopted, the new band is steady state again.
    await tick()
    await tick()
    assert len(calls) == 1, "an unchanged band must not re-run the update on later ticks"


@pytest.mark.asyncio
async def test_editing_the_running_blocks_band_updates_in_place(client, fake_ha, tick) -> None:
    """The same thing via an edit rather than a crossing: PUTting a new band on
    the block the cycle is running under is picked up on the next tick."""
    _seed_heating_thermostat(fake_ha)
    room_id, sched_id = await _heating_room(
        client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", 68.0
    )
    await client.put(f"/api/rooms/{room_id}/schedules/{sched_id}", json={"deadband_override": 1.0})

    await tick()
    assert _active_band(client, room_id) == 1.0
    calls = _spy_on_cycle_updates(client)

    await client.put(f"/api/rooms/{room_id}/schedules/{sched_id}", json={"deadband_override": 2.0})
    await tick()

    assert len(calls) == 1
    assert _active_band(client, room_id) == 2.0
    assert len(await _open_cycles(client)) == 1


@pytest.mark.asyncio
async def test_steady_state_band_causes_no_cycle_churn(client, fake_ha, tick) -> None:
    """The #408-class guard: a band that does not move must NOT re-run
    _start_or_update_cycle, on any number of subsequent ticks."""
    _seed_heating_thermostat(fake_ha)
    room_id, sched_id = await _heating_room(
        client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", 68.0
    )
    await client.put(f"/api/rooms/{room_id}/schedules/{sched_id}", json={"deadband_override": 2.0})

    await tick()
    assert _active_band(client, room_id) == 2.0
    calls = _spy_on_cycle_updates(client)

    for _ in range(4):
        await tick()

    assert calls == [], f"a stable band must not re-run the cycle update; got {len(calls)} calls"
    assert len(await _logs(client)) == 1
    assert _engine_state(client) == "running"


@pytest.mark.asyncio
async def test_bandless_block_also_causes_no_churn(client, fake_ha, tick) -> None:
    """A block with NO band holds None on both sides of the comparison — None
    != None must never be true, or every pre-#517 install would churn."""
    _seed_heating_thermostat(fake_ha)
    room_id, _ = await _heating_room(client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", 68.0)

    await tick()
    assert _active_band(client, room_id) is None
    calls = _spy_on_cycle_updates(client)

    for _ in range(4):
        await tick()

    assert calls == []
    assert _engine_state(client) == "running"


@pytest.mark.asyncio
async def test_clearing_the_band_to_null_updates_in_place(client, fake_ha, tick) -> None:
    """The other direction: value → None (the block's band is cleared, or the
    block ends and a bandless one takes over) must also fire."""
    _seed_heating_thermostat(fake_ha)
    room_id, sched_id = await _heating_room(
        client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", 68.0
    )
    await client.put(f"/api/rooms/{room_id}/schedules/{sched_id}", json={"deadband_override": 2.0})

    await tick()
    assert _active_band(client, room_id) == 2.0
    calls = _spy_on_cycle_updates(client)

    await client.put(f"/api/rooms/{room_id}/schedules/{sched_id}", json={"deadband_override": None})
    await tick()

    assert len(calls) == 1, "clearing the band must re-run _start_or_update_cycle"
    assert _active_band(client, room_id) is None, "the cycle must fall back to inheriting"
    assert len(await _open_cycles(client)) == 1


@pytest.mark.asyncio
async def test_setting_a_band_on_a_bandless_running_block_updates_in_place(
    client, fake_ha, tick
) -> None:
    """And None → value."""
    _seed_heating_thermostat(fake_ha)
    room_id, sched_id = await _heating_room(
        client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", 68.0
    )

    await tick()
    assert _active_band(client, room_id) is None
    calls = _spy_on_cycle_updates(client)

    await client.put(f"/api/rooms/{room_id}/schedules/{sched_id}", json={"deadband_override": 2.0})
    await tick()

    assert len(calls) == 1
    assert _active_band(client, room_id) == 2.0


@pytest.mark.asyncio
async def test_band_going_to_zero_updates_in_place(client, fake_ha, tick) -> None:
    """0.0 is falsy but is a real band — `2.0 → 0.0` must be seen as a change,
    and 0.0 must land on the ActiveRoom rather than collapsing to None."""
    _seed_heating_thermostat(fake_ha)
    room_id, sched_id = await _heating_room(
        client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", 68.0
    )
    await client.put(f"/api/rooms/{room_id}/schedules/{sched_id}", json={"deadband_override": 2.0})

    await tick()
    calls = _spy_on_cycle_updates(client)

    await client.put(f"/api/rooms/{room_id}/schedules/{sched_id}", json={"deadband_override": 0})
    await tick()

    assert len(calls) == 1
    assert _active_band(client, room_id) == 0.0


@pytest.mark.asyncio
async def test_override_taking_over_from_a_banded_block_drops_the_band(
    client, fake_ha, tick
) -> None:
    """An override outranks the block and carries no band of its own, so the
    running cycle must fall back to the room→thermostat chain."""
    _seed_heating_thermostat(fake_ha)
    room_id, sched_id = await _heating_room(
        client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", 68.0
    )
    await client.put(f"/api/rooms/{room_id}/schedules/{sched_id}", json={"deadband_override": 2.0})

    await tick()
    assert _active_band(client, room_id) == 2.0

    # Same 68°F target — only the source (and hence the band) changes.
    await client.post(
        f"/api/rooms/{room_id}/override", json={"target_temp": 68.0, "duration_hours": 2}
    )
    await tick()

    assert _active_band(client, room_id) is None
    open_cycles = await _open_cycles(client)
    assert len(open_cycles) == 1, "the handoff must not tear the cycle down"
    assert next(iter(open_cycles[0]["rooms"].values()))["source"] == "override"
