"""Outdoor-temperature cooling lockout integration tests (Issue #209).

Running a standard AC compressor when it is cold outside risks liquid
slugging and evaporator coil icing. These tests drive the full engine
against a fake Home Assistant and verify:

  - a cooling cycle is suppressed while the outdoor sensor reads below the
    configured ``cooling_lockout_below_f`` threshold;
  - cooling proceeds normally when it is warm enough outside;
  - the lockout fails open (cooling allowed, with a warning) when a threshold
    is configured but no outdoor sensor is set up;
  - the new config field round-trips through the REST API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"
OUTDOOR = "sensor.outdoor_temp"


async def _create_cooling_room(client) -> None:
    """Create a warm room on an all-day schedule — a cooling cycle is wanted."""
    resp = await client.post("/api/rooms", json={"name": "Bedroom", "thermostat_entity_id": THERMO})
    room_id = (await resp.json())["id"]
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
            "target_temp": 72.0,
        },
    )


def _seed_warm_room(fake_ha) -> None:
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 78.0, "temperature": 76.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state("sensor.test_room_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.test_room_vent", "open", {})


async def _event_messages(client, level: str) -> list[str]:
    events = await (await client.get(f"/api/logs/events?level={level}")).json()
    return [e["message"] for e in events]


@pytest.mark.asyncio
async def test_cooling_suppressed_when_outdoor_below_lockout(client, fake_ha, tick) -> None:
    """Cold outside → the cooling cycle is locked out and does not start."""
    _seed_warm_room(fake_ha)
    fake_ha.seed_state(OUTDOOR, "40.0", {"unit_of_measurement": "°F"})
    await _create_cooling_room(client)

    # House-wide outdoor sensor + per-thermostat lockout threshold.
    assert (
        await client.put("/api/settings/outside-temp-entity", json={"entity_id": OUTDOOR})
    ).status == 200
    assert (
        await client.put(f"/api/thermostats/{THERMO}", json={"cooling_lockout_below_f": 55})
    ).status == 200

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 0, "no cooling cycle should start while locked out"
    assert client.app["scheduler"]._engines[THERMO].cycle_state.value == "idle"

    warnings = await _event_messages(client, "warning")
    assert any("suppressed" in m and "outdoor" in m.lower() for m in warnings), warnings


@pytest.mark.asyncio
async def test_cooling_proceeds_when_outdoor_above_lockout(client, fake_ha, tick) -> None:
    """Warm enough outside → cooling proceeds normally (no regression)."""
    _seed_warm_room(fake_ha)
    fake_ha.seed_state(OUTDOOR, "70.0", {"unit_of_measurement": "°F"})
    await _create_cooling_room(client)

    await client.put("/api/settings/outside-temp-entity", json={"entity_id": OUTDOOR})
    await client.put(f"/api/thermostats/{THERMO}", json={"cooling_lockout_below_f": 55})

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1, "cooling cycle should start when it is warm enough outside"
    assert logs[0]["mode"] == "cooling"


@pytest.mark.asyncio
async def test_cooling_fails_open_when_outdoor_sensor_unconfigured(client, fake_ha, tick) -> None:
    """Threshold set but no outdoor sensor → fail open: cooling proceeds, warn."""
    _seed_warm_room(fake_ha)
    await _create_cooling_room(client)

    # Lockout threshold configured, but no outside-temp entity is set up.
    await client.put(f"/api/thermostats/{THERMO}", json={"cooling_lockout_below_f": 55})

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1, "lockout must fail open when no outdoor sensor is configured"

    warnings = await _event_messages(client, "warning")
    assert any("unset or unreadable" in m for m in warnings), warnings


@pytest.mark.asyncio
async def test_cooling_lockout_config_roundtrips_through_api(client, fake_ha) -> None:
    """cooling_lockout_below_f persists through the REST API, and null clears it."""
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"cooling_lockout_below_f": 55})
    assert resp.status == 200
    assert (await resp.json())["cooling_lockout_below_f"] == 55

    listing = await (await client.get("/api/thermostats")).json()
    entry = next(t for t in listing if t["thermostat_entity_id"] == THERMO)
    assert entry["cooling_lockout_below_f"] == 55

    # Passing null disables the lockout.
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"cooling_lockout_below_f": None})
    assert resp.status == 200
    assert (await resp.json())["cooling_lockout_below_f"] is None
