"""
Regression test for Issue #296: an IDLE engine whose rooms are all within
deadband resets the thermostat setpoint to the current ambient so the HVAC
goes idle — but it must not re-command that same setpoint on every tick.

Before the fix, every 60 s tick fired a `climate.set_temperature` service call
even though nothing changed, churning the HA recorder and risking vendor rate
limits for cloud thermostats.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


async def _make_idle_room_at_target(client, fake_ha) -> None:
    thermostat = "climate.test_thermostat"
    sensor = "sensor.room_temp"
    vent = "cover.room_vent"

    # Thermostat ambient 72°F, but its setpoint is parked at 68°F (differs from
    # ambient), so the first idle tick will reset it to ambient.
    fake_ha.seed_state(
        thermostat,
        "cool",
        {"current_temperature": 72.0, "temperature": 68.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state(sensor, "72.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(vent, "open", {})

    resp = await client.post(
        "/api/rooms", json={"name": "Room", "thermostat_entity_id": thermostat}
    )
    room_id = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": sensor})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": vent, "control_method": "open_close"},
    )
    # Schedule active now targeting 72 — the room is "active" but already at
    # target, so the engine infers mode "off".
    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).time().replace(second=0, microsecond=0)
    end = (now + timedelta(hours=1)).time().replace(second=0, microsecond=0)
    await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={
            "days_of_week": list(range(7)),
            "start_time": start.isoformat(timespec="minutes"),
            "end_time": end.isoformat(timespec="minutes"),
            "target_temp": 72.0,
        },
    )


@pytest.mark.asyncio
async def test_idle_setpoint_not_recommanded_every_tick(client, fake_ha, tick) -> None:
    thermostat = "climate.test_thermostat"
    await _make_idle_room_at_target(client, fake_ha)

    # First tick: parked setpoint for mode "cool" is ambient 72 + overshoot 2
    # = 74 ≠ current 68 → one reset to the parked value. Parking above ambient
    # keeps the thermostat from restarting the HVAC on its own room's drift.
    await tick()
    sets = [c for c in fake_ha.calls_for("set_temperature") if c.data["entity_id"] == thermostat]
    assert len(sets) == 1, f"first idle tick should reset setpoint once; got {sets}"
    assert sets[0].data["temperature"] == pytest.approx(74.0)

    # Second tick: setpoint already parked → no further service call.
    await tick()
    sets = [c for c in fake_ha.calls_for("set_temperature") if c.data["entity_id"] == thermostat]
    assert len(sets) == 1, f"idle setpoint must not be re-commanded when already parked; got {sets}"
