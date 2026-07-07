"""Self-repair for active rooms that lost their RoomCycleState (#427).

An exception between committing the active-room map and the per-room work in
``_start_or_update_cycle`` (a locked DB is the known case, #286) leaves a
room "active" with no cycle state and no opened vent. The tick retry never
redoes the work — ``added`` is computed against the already-updated map — so
the zombie room was never conditioned and its missing state blocked
termination until the cycle timeout.

``_monitor_rooms`` now repairs the inconsistency: it recreates the room's
cycle state, opens its vents, logs loudly, and monitoring resumes the same
tick.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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


@pytest.mark.asyncio
async def test_zombie_room_is_repaired_and_cycle_can_terminate(client, fake_ha, tick) -> None:
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 80.0, "temperature": 78.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state(A_SENSOR, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(B_SENSOR, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(A_VENT, "open", {})
    fake_ha.seed_state(B_VENT, "open", {})
    room_a = await _make_room(client, "RoomA", A_SENSOR, A_VENT, 72.0)
    await _make_room(client, "RoomB", B_SENSOR, B_VENT, 72.0)

    await tick()  # cooling cycle starts with both rooms
    eng = _engine(client)
    assert eng.cycle_state.value == "running"
    assert room_a in eng._room_cycle_states

    # Simulate the #427 inconsistency: RoomA's join was interrupted after the
    # active map was committed — no cycle state, vent never opened.
    del eng._room_cycle_states[room_a]
    await fake_ha.set_entity_state(A_VENT, "closed", {})

    fake_ha.reset_calls()
    await tick()

    # Repair: state recreated, vent re-opened, warning surfaced.
    assert room_a in eng._room_cycle_states, "missing RoomCycleState must be recreated"
    assert fake_ha.get_state(A_VENT)["state"] == "open", "the zombie room's vent must open"
    events = await (await client.get("/api/logs/events?limit=20")).json()
    assert any("Repaired missing cycle state" in e["message"] for e in events)

    # And the cycle can now terminate normally once both rooms reach target —
    # previously the missing state pinned all_at_target=False until timeout.
    await fake_ha.set_entity_state(A_SENSOR, "72.0", {"unit_of_measurement": "°F"})
    await fake_ha.set_entity_state(B_SENSOR, "72.0", {"unit_of_measurement": "°F"})
    await tick()
    assert eng.cycle_state.value == "idle", "repaired cycle must terminate normally"
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is not None
