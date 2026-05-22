"""
Regression test: a schedule block must preempt an in-flight presence cycle.

Scenario:
  - Room has presence_holdover_hours>0 and a system_wide_temp (presence target).
  - Presence fires → cycle starts with source=presence at the presence target.
  - A schedule with a different target becomes active for the same room.
  - The schedule (higher priority than presence in _resolve_room) must take
    over. Since #215 this is an *in-place update* of the running cycle: the
    same cycle keeps running with its source/target updated, rather than being
    torn down and a fresh one started. The schedule and presence targets here
    are both heating demands, so the direction does not flip.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.mark.asyncio
async def test_schedule_preempts_running_presence_cycle(client, fake_ha, tick) -> None:
    # Heating scenario: ambient below both presence (70) and schedule (68) targets.
    fake_ha.seed_state(
        "climate.test_thermostat",
        "heat",
        {
            "current_temperature": 65.0,
            "temperature": 65.0,
            "hvac_action": "idle",
        },
    )
    fake_ha.seed_state("sensor.test_room_temp", "65.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.test_room_vent", "closed", {})
    fake_ha.seed_state("binary_sensor.test_room_presence", "off", {})

    # Room with presence target 70 and no schedule yet.
    resp = await client.post(
        "/api/rooms",
        json={
            "name": "Bedroom",
            "thermostat_entity_id": "climate.test_thermostat",
            "system_wide_temp": 70.0,
            "presence_holdover_hours": 2.0,
        },
    )
    room_id = (await resp.json())["id"]

    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.test_room_temp"})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": "cover.test_room_vent", "control_method": "open_close"},
    )
    await client.post(
        f"/api/rooms/{room_id}/presence",
        json={"entity_id": "binary_sensor.test_room_presence"},
    )

    # Trigger presence → scheduler picks it up via subscribe_all and starts a
    # cycle with source=presence at the 70°F presence target.
    await fake_ha.set_entity_state("binary_sensor.test_room_presence", "on", {})
    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1, "presence should have started exactly one cycle"
    presence_cycle_id = logs[0]["id"]
    assert logs[0]["ended_at"] is None
    # rooms is a dict keyed by room_id → {name, target, source}.
    presence_rooms = logs[0]["rooms"]
    assert next(iter(presence_rooms.values()))["source"] == "presence"
    assert next(iter(presence_rooms.values()))["target"] == 70.0

    # Now add a schedule covering "now" with a lower target. Schedule has higher
    # priority than presence in _resolve_room, so the next tick should detect
    # the trigger change and terminate the presence cycle.
    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).time().replace(second=0, microsecond=0)
    end = (now + timedelta(hours=1)).time().replace(second=0, microsecond=0)
    await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={
            "days_of_week": list(range(7)),
            "start_time": start.isoformat(timespec="minutes"),
            "end_time": end.isoformat(timespec="minutes"),
            "target_temp": 68.0,
        },
    )

    await tick()

    # Since #215 the schedule preempts the presence trigger via an in-place
    # update: the SAME cycle keeps running — no teardown, no second log — with
    # its source/target updated to reflect the schedule.
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1, "no teardown — exactly one cycle log should exist"
    cycle = logs[0]
    assert cycle["id"] == presence_cycle_id, "the same cycle must continue running"
    assert cycle["ended_at"] is None, "the cycle must still be open"

    # The cycle log reflects the schedule handoff: source=schedule, target=68.
    room_entry = next(iter(cycle["rooms"].values()))
    assert room_entry["source"] == "schedule"
    assert room_entry["target"] == 68.0

    # Latest setpoint write should reflect the schedule target (68°F),
    # not the old presence target (70°F). In heating the engine writes
    # target + overshoot_delta (default 2.0) so expect 70.0.
    setpoint_calls = fake_ha.calls_for("set_temperature")
    assert setpoint_calls, "engine should write a setpoint for the updated cycle"
    last_setpoint = setpoint_calls[-1].data["temperature"]
    assert last_setpoint == pytest.approx(70.0, abs=0.5), (
        f"setpoint {last_setpoint} should match schedule target 68 + overshoot"
    )
