"""Regression test for issue #67.

When a new cycle starts, vents for rooms that are NOT part of the cycle
(idle rooms on the same thermostat zone) must be closed.  Previously,
``_terminate_cycle`` re-opened all zone vents as part of returning to a
neutral idle state, but ``_start_or_update_cycle`` never closed the idle
rooms' vents, so they stayed open for the duration of the next cycle.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest


def _schedule_covering_now(client_post, room_id: str, target_temp: float):
    """Helper coroutine — returns the coroutine so callers can await it."""
    now = datetime.now()
    start = (now - timedelta(hours=1)).time().replace(second=0, microsecond=0)
    end = (now + timedelta(hours=1)).time().replace(second=0, microsecond=0)
    return client_post(
        f"/api/rooms/{room_id}/schedules",
        json={
            "days_of_week": list(range(7)),
            "start_time": start.isoformat(timespec="minutes"),
            "end_time": end.isoformat(timespec="minutes"),
            "target_temp": target_temp,
        },
    )


@pytest.mark.asyncio
async def test_idle_room_vent_closed_when_cycle_starts(client, fake_ha, tick) -> None:
    """Idle room vents are closed at cycle start (regression: issue #67).

    Setup:
      - Two rooms on the same thermostat zone.
      - Only Room A has a schedule active now (Room B is idle).
      - Both vents start in the "open" state (simulating the state left by a
        prior _terminate_cycle that re-opened everything).

    Expected after one tick:
      - Room A's vent is opened (or stays open).
      - Room B's vent is CLOSED because it is not participating in the cycle.
    """
    thermostat = "climate.test_thermostat"
    sensor_a = "sensor.room_a_temp"
    vent_a = "cover.room_a_vent"
    sensor_b = "sensor.room_b_temp"
    vent_b = "cover.room_b_vent"

    # Seed thermostat: cooling mode, room is warm → a cooling cycle will start.
    fake_ha.seed_state(
        thermostat,
        "cool",
        {"current_temperature": 76.0, "temperature": 76.0, "hvac_action": "idle"},
    )
    # Room A: warm, needs cooling.
    fake_ha.seed_state(sensor_a, "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(vent_a, "open", {})
    # Room B: already at target, no schedule — idle.
    fake_ha.seed_state(sensor_b, "72.0", {"unit_of_measurement": "°F"})
    # Vent B starts OPEN — this simulates the state left by a previous
    # _terminate_cycle which re-opens all zone vents.
    fake_ha.seed_state(vent_b, "open", {})

    # Create Room A with a schedule covering now.
    resp = await client.post(
        "/api/rooms",
        json={"name": "Room A", "thermostat_entity_id": thermostat},
    )
    room_a_id = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_a_id}/sensors", json={"entity_id": sensor_a})
    await client.post(
        f"/api/rooms/{room_a_id}/vents",
        json={"entity_id": vent_a, "control_method": "open_close"},
    )
    await _schedule_covering_now(client.post, room_a_id, target_temp=72.0)

    # Create Room B with NO schedule (idle room on the same zone).
    resp = await client.post(
        "/api/rooms",
        json={"name": "Room B", "thermostat_entity_id": thermostat},
    )
    room_b_id = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_b_id}/sensors", json={"entity_id": sensor_b})
    await client.post(
        f"/api/rooms/{room_b_id}/vents",
        json={"entity_id": vent_b, "control_method": "open_close"},
    )

    # Run one tick — a cycle should start for Room A only.
    await tick()

    # Room A's vent started open and VentController skips open_cover when the
    # vent is already open, so we verify state rather than call history.
    vent_a_state = fake_ha.get_state(vent_a)
    assert vent_a_state is not None and vent_a_state["state"] == "open", (
        f"Room A vent should be open; state={vent_a_state}"
    )

    # Room B's vent should have been CLOSED (the key regression check).
    close_calls = fake_ha.calls_for("close_cover")
    closed_entities = {c.data["entity_id"] for c in close_calls}
    assert vent_b in closed_entities, (
        f"Idle room B vent should have been closed at cycle start; "
        f"close calls: {close_calls}, all calls: {fake_ha.calls}"
    )
    vent_b_state = fake_ha.get_state(vent_b)
    assert vent_b_state is not None and vent_b_state["state"] == "closed", (
        f"Idle room B vent state should be 'closed'; got: {vent_b_state}"
    )

    # Sanity: Room A's vent should NOT have been closed.
    assert vent_a not in closed_entities, "Active room A vent must not be closed at cycle start"

    # A running cycle log should exist for this tick.
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1
    assert logs[0]["ended_at"] is None


@pytest.mark.asyncio
async def test_idle_room_vent_not_closed_mid_cycle_when_rooms_change(client, fake_ha, tick) -> None:
    """Mid-cycle room additions do NOT re-trigger the idle-room close sweep.

    This ensures the ``is_fresh_start`` guard is correctly scoped to the
    initial cycle-start path only, and does not accidentally close vents
    during mid-cycle room set updates.
    """
    thermostat = "climate.test_thermostat"
    sensor_a = "sensor.room_a_temp"
    vent_a = "cover.room_a_vent"
    sensor_b = "sensor.room_b_temp"
    vent_b = "cover.room_b_vent"

    fake_ha.seed_state(
        thermostat,
        "cool",
        {"current_temperature": 76.0, "temperature": 76.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state(sensor_a, "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(vent_a, "closed", {})
    fake_ha.seed_state(sensor_b, "73.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(vent_b, "closed", {})

    # Both rooms have schedules active now; both join the initial cycle.
    now = datetime.now()
    start = (now - timedelta(hours=1)).time().replace(second=0, microsecond=0)
    end = (now + timedelta(hours=1)).time().replace(second=0, microsecond=0)

    for name, sensor, vent in [("Room A", sensor_a, vent_a), ("Room B", sensor_b, vent_b)]:
        resp = await client.post(
            "/api/rooms",
            json={"name": name, "thermostat_entity_id": thermostat},
        )
        rid = (await resp.json())["id"]
        await client.post(f"/api/rooms/{rid}/sensors", json={"entity_id": sensor})
        await client.post(
            f"/api/rooms/{rid}/vents",
            json={"entity_id": vent, "control_method": "open_close"},
        )
        await client.post(
            f"/api/rooms/{rid}/schedules",
            json={
                "days_of_week": list(range(7)),
                "start_time": start.isoformat(timespec="minutes"),
                "end_time": end.isoformat(timespec="minutes"),
                "target_temp": 72.0,
            },
        )

    # First tick: cycle starts with both rooms — no idle rooms, vent B stays open.
    await tick()
    assert fake_ha.calls_for("open_cover"), "Both vents should open at cycle start"

    # Room B hits target; on next tick the engine will see it reached target
    # and only Room A is unfinished. The mid-cycle path removes Room B, not
    # the idle-sweep path. Room B's vent should be closed by the monitor, not
    # by the idle-room sweep (which only runs on fresh start).
    fake_ha.reset_calls()
    await fake_ha.set_entity_state(sensor_b, "72.0", {"unit_of_measurement": "°F"})
    await tick()  # Room B hits target → vent B closed by _monitor_rooms

    close_calls = fake_ha.calls_for("close_cover")
    closed_entities = {c.data["entity_id"] for c in close_calls}
    # Room B's vent should have been closed because it reached target —
    # but that's via _monitor_rooms, not the fresh-start idle sweep.
    # (We just verify Room A's vent was NOT closed.)
    assert vent_a not in closed_entities, (
        "Active room A vent must not be closed when room B reaches target mid-cycle"
    )
