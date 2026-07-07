"""Post-cycle parked setpoint — the idle-side mirror of overshoot.

Terminating a cycle used to set the thermostat setpoint to its own ambient.
That stops the HVAC, but leaves the thermostat ARMED: the moment the room the
thermostat lives in drifts past its native hysteresis, the HVAC restarts on
the thermostat's own judgement — racing the engine, which wants room demand
to start the next cycle (and whose vents may be positioned for a completely
different room set).

The fix parks the setpoint ``overshoot_delta`` (user-configurable, the same
knob that drives the mid-cycle ambient-anchored overshoot) to the idle side
of the cycle direction:

  - heating: overshoot 2, requested 70, heats to 70 → parked at ``heat 68``
    — the HVAC stays idle unless the zone genuinely falls below 68, by which
    point the engine has already reacted to room demand;
  - cooling is the mirror: parked at ambient + overshoot.

These tests drive full cycles and pin the parked value at termination for
both directions, including a non-default overshoot to prove the knob is
honoured rather than a hard-coded 2.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"
SENSOR = "sensor.room_temp"
VENT = "cover.room_vent"


async def _create_room_with_schedule(client, target_temp: float) -> str:
    resp = await client.post("/api/rooms", json={"name": "Room", "thermostat_entity_id": THERMO})
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": SENSOR})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": VENT, "control_method": "open_close"},
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
            "target_temp": target_temp,
        },
    )
    return room_id


def _thermo_setpoints(fake_ha) -> list[float]:
    return [
        c.data["temperature"]
        for c in fake_ha.calls_for("set_temperature")
        if c.data["entity_id"] == THERMO
    ]


@pytest.mark.asyncio
async def test_heating_cycle_parks_setpoint_below_ambient(client, fake_ha, tick) -> None:
    """Overshoot 2, requesting 70, room heats 68 → 70: on termination the
    thermostat must be commanded to heat 68 (ambient 70 − overshoot 2), NOT
    ambient — so it stays idle unless the zone truly falls below 68."""
    fake_ha.seed_state(
        THERMO,
        "heat",
        {"current_temperature": 70.0, "temperature": 68.0, "hvac_action": "heating"},
    )
    fake_ha.seed_state(SENSOR, "68.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})
    await _create_room_with_schedule(client, target_temp=70.0)
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"overshoot_delta": 2.0})
    assert resp.status == 200

    await tick()  # heating cycle starts
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["mode"] == "heating"

    # Room reaches 70 → cycle terminates.
    await fake_ha.set_entity_state(SENSOR, "70.0", {"unit_of_measurement": "°F"})
    await tick()
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is not None, "cycle should have completed"

    parked = _thermo_setpoints(fake_ha)[-1]
    assert parked == pytest.approx(68.0), (
        f"termination must park the setpoint at ambient 70 − overshoot 2 = 68, got {parked}"
    )
    # And the mode stays heat — the margin only means the HVAC won't
    # self-trigger until the zone falls a full overshoot below current ambient.
    last_call = [c for c in fake_ha.calls_for("set_temperature") if c.data["entity_id"] == THERMO][
        -1
    ]
    assert last_call.data.get("hvac_mode") == "heat"


@pytest.mark.asyncio
async def test_cooling_cycle_parks_setpoint_above_ambient_with_configured_overshoot(
    client, fake_ha, tick
) -> None:
    """Cooling mirror with a NON-default overshoot (3.0): termination parks at
    ambient + 3.0 — proving the configured knob is used, not a constant 2."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 72.0, "temperature": 74.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state(SENSOR, "74.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})
    await _create_room_with_schedule(client, target_temp=70.0)
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"overshoot_delta": 3.0})
    assert resp.status == 200

    await tick()  # cooling cycle starts
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["mode"] == "cooling"

    await fake_ha.set_entity_state(SENSOR, "70.0", {"unit_of_measurement": "°F"})
    await tick()
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is not None, "cycle should have completed"

    parked = _thermo_setpoints(fake_ha)[-1]
    assert parked == pytest.approx(75.0), (
        f"termination must park the setpoint at ambient 72 + overshoot 3 = 75, got {parked}"
    )


@pytest.mark.asyncio
async def test_abort_parks_setpoint_on_idle_side_too(client, fake_ha, tick) -> None:
    """Aborts stop the HVAC the same way termination does — the parked margin
    must apply there too, or a system-disable mid-cycle re-arms the race."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 76.0, "temperature": 74.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state(SENSOR, "76.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})
    await _create_room_with_schedule(client, target_temp=70.0)
    await client.put(f"/api/thermostats/{THERMO}", json={"overshoot_delta": 2.0})

    await tick()  # cooling cycle starts
    eng = client.app["scheduler"]._engines[THERMO]
    assert eng.cycle_state.value == "running"

    fake_ha.reset_calls()
    await client.app["scheduler"].set_system_enabled(False)

    assert eng.cycle_state.value == "idle", "system disable must abort the cycle"
    parked = _thermo_setpoints(fake_ha)[-1]
    assert parked == pytest.approx(78.0), (
        f"abort must park the setpoint at ambient 76 + overshoot 2 = 78, got {parked}"
    )


@pytest.mark.asyncio
async def test_fractional_ambient_parks_at_whole_degree_without_churn(
    client, fake_ha, tick
) -> None:
    """The production drift-churn case: ambient 72.28 °F + overshoot 2 used to
    park at 74.28 °F, which a whole-degree thermostat stores as 74 — the
    reconciler then saw permanent drift and re-asserted every pass. The parked
    value must be the rounded 74, and the following idle tick must NOT
    re-command it (idempotent skip)."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 72.28, "temperature": 74.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state(SENSOR, "74.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})
    await _create_room_with_schedule(client, target_temp=70.0)
    await client.put(f"/api/thermostats/{THERMO}", json={"overshoot_delta": 2.0})

    await tick()  # cooling cycle starts
    await fake_ha.set_entity_state(SENSOR, "70.0", {"unit_of_measurement": "°F"})
    await tick()  # target reached → terminate + park
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is not None

    parked = _thermo_setpoints(fake_ha)[-1]
    assert parked == pytest.approx(74.0), (
        f"ambient 72.28 + overshoot 2 = 74.28 must park at the whole degree 74, got {parked}"
    )

    # The thermostat now reports exactly what we commanded — the next idle
    # tick must send nothing (before the fix it re-asserted 74.28 forever).
    fake_ha.reset_calls()
    await tick()
    assert not _thermo_setpoints(fake_ha), (
        "an already-parked whole-degree setpoint must not be re-commanded"
    )
