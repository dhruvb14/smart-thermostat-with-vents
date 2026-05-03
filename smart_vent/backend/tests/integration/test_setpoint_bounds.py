"""Thermostat safety bounds: setpoint never escapes [min_setpoint, max_setpoint].

Cooling clamps setpoint *below* ambient (by overshoot_delta) to force the
HVAC to actually run — but on a cool day with a high min_setpoint that
aggressive clamp can drive the commanded setpoint below the configured
safety floor. Symmetrically for heating. The engine must also clamp to
the configured bounds so a cooling cycle cannot ask the HVAC to chill
below min_setpoint, and a heating cycle cannot ask it to warm above
max_setpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


async def _make_room_with_schedule(
    client,
    *,
    target_temp: float,
    thermostat: str = "climate.test_thermostat",
) -> str:
    resp = await client.post(
        "/api/rooms",
        json={"name": "Bedroom", "thermostat_entity_id": thermostat},
    )
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.test_room_temp"})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": "cover.test_room_vent", "control_method": "open_close"},
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


@pytest.mark.asyncio
async def test_cooling_setpoint_clamped_to_min_setpoint(client, fake_ha, tick) -> None:
    """Configure a high floor; cooling cycle must NOT drop the setpoint below it."""
    # min_setpoint=70 means "never command the thermostat below 70°F".
    await client.post(
        "/api/thermostats",
        json={
            "thermostat_entity_id": "climate.test_thermostat",
            "min_setpoint": 70.0,
            "max_setpoint": 85.0,
            "overshoot_delta": 4.0,  # aggressive — would land below 70 without the clamp
        },
    )

    # Thermostat reads 72°F ambient. Without the bounds clamp, cooling would
    # drive setpoint to ambient - overshoot_delta = 68°F, below min=70.
    fake_ha.seed_state(
        "climate.test_thermostat",
        "cool",
        {"current_temperature": 72.0, "temperature": 72.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state("sensor.test_room_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.test_room_vent", "closed", {})

    await _make_room_with_schedule(client, target_temp=65.0)
    await tick()

    sp_calls = fake_ha.calls_for("set_temperature")
    assert sp_calls, f"no setpoint written; got {fake_ha.calls}"
    for call in sp_calls:
        assert call.data["temperature"] >= 70.0, (
            f"setpoint {call.data['temperature']} breached min_setpoint=70: {call.data}"
        )


@pytest.mark.asyncio
async def test_heating_setpoint_clamped_to_max_setpoint(client, fake_ha, tick) -> None:
    """Configure a low ceiling; heating cycle must NOT raise setpoint above it."""
    await client.post(
        "/api/thermostats",
        json={
            "thermostat_entity_id": "climate.test_thermostat",
            "min_setpoint": 55.0,
            "max_setpoint": 75.0,
            "overshoot_delta": 4.0,
        },
    )

    # Hot-thermostat/cold-room: heating cycle would target ambient + 4 = 82,
    # above the 75°F ceiling.
    fake_ha.seed_state(
        "climate.test_thermostat",
        "heat",
        {"current_temperature": 78.0, "temperature": 78.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state("sensor.test_room_temp", "60.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.test_room_vent", "closed", {})

    await _make_room_with_schedule(client, target_temp=85.0)
    await tick()

    sp_calls = fake_ha.calls_for("set_temperature")
    assert sp_calls, f"no setpoint written; got {fake_ha.calls}"
    for call in sp_calls:
        assert call.data["temperature"] <= 75.0, (
            f"setpoint {call.data['temperature']} breached max_setpoint=75: {call.data}"
        )


@pytest.mark.asyncio
async def test_external_setpoint_drift_outside_bounds_is_flagged(client, fake_ha, tick) -> None:
    """If an external actor sets the HA setpoint outside the configured bounds
    while the engine is idle, the reconcile loop must surface a warning so
    the user notices — silent drift past safety bounds is the failure mode
    we want to catch."""
    await client.post(
        "/api/thermostats",
        json={
            "thermostat_entity_id": "climate.test_thermostat",
            "min_setpoint": 65.0,
            "max_setpoint": 80.0,
            "reconciliation_interval_min": 1,  # fire reconcile every tick
        },
    )

    # A room must exist so the scheduler creates a CycleEngine for this
    # thermostat — but we skip the schedule so no cycle starts and the
    # engine stays in IDLE, exercising the idle reconcile path.
    resp = await client.post(
        "/api/rooms",
        json={"name": "Bedroom", "thermostat_entity_id": "climate.test_thermostat"},
    )
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.test_room_temp"})

    # Seed a thermostat with a dangerously low setpoint (50°F) that an
    # external actor has dialed in.
    fake_ha.seed_state(
        "climate.test_thermostat",
        "cool",
        {"current_temperature": 72.0, "temperature": 50.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state("sensor.test_room_temp", "72.0", {"unit_of_measurement": "°F"})

    await tick()

    # The engine must NOT have treated the external value as "OK". It should
    # have at least flagged the drift via the event logger.
    resp = await client.get("/api/logs/events?limit=100&category=reconcile")
    events = await resp.json()
    bounds_warnings = [e for e in events if "bounds" in (e.get("message") or "").lower()]
    assert bounds_warnings, f"expected bounds-drift reconcile event; got {events}"
