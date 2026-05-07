"""Regression tests for API input validation bounds."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_room_temperature_bounds(client) -> None:
    """Verify system_wide_temp is restricted to 40°F–90°F."""
    # Too low (POST)
    resp = await client.post(
        "/api/rooms",
        json={"name": "Cold Room", "thermostat_entity_id": "climate.test", "system_wide_temp": 39},
    )
    assert resp.status == 400
    assert "target temperature" in (await resp.json())["error"].lower()

    # Too high (POST)
    resp = await client.post(
        "/api/rooms",
        json={"name": "Hot Room", "thermostat_entity_id": "climate.test", "system_wide_temp": 91},
    )
    assert resp.status == 400

    # Valid POST then invalid PUT
    resp = await client.post(
        "/api/rooms",
        json={"name": "Valid Room", "thermostat_entity_id": "climate.test", "system_wide_temp": 70},
    )
    room_id = (await resp.json())["id"]

    resp = await client.put(f"/api/rooms/{room_id}", json={"system_wide_temp": 95})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_schedule_temperature_bounds(client) -> None:
    """Verify schedule target_temp is restricted to 40°F–90°F."""
    resp = await client.post(
        "/api/rooms",
        json={"name": "Test Room", "thermostat_entity_id": "climate.test"},
    )
    room_id = (await resp.json())["id"]

    # Too low (POST)
    resp = await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={"days_of_week": [0], "start_time": "08:00", "end_time": "09:00", "target_temp": 35},
    )
    assert resp.status == 400
    assert "target temperature" in (await resp.json())["error"].lower()

    # Valid POST then invalid PUT
    resp = await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={"days_of_week": [0], "start_time": "08:00", "end_time": "09:00", "target_temp": 70},
    )
    sched_id = (await resp.json())["id"]
    resp = await client.put(f"/api/rooms/{room_id}/schedules/{sched_id}", json={"target_temp": 30})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_override_bounds(client) -> None:
    """Verify override target_temp and duration are restricted."""
    resp = await client.post(
        "/api/rooms",
        json={"name": "Test Room", "thermostat_entity_id": "climate.test"},
    )
    room_id = (await resp.json())["id"]

    # Temp too high
    resp = await client.post(
        f"/api/rooms/{room_id}/override", json={"target_temp": 100, "duration_hours": 1}
    )
    assert resp.status == 400

    # Duration too long (over 1 year)
    resp = await client.post(
        f"/api/rooms/{room_id}/override", json={"target_temp": 70, "duration_hours": 9000}
    )
    assert resp.status == 400
    assert "duration" in (await resp.json())["error"].lower()

    # Duration negative
    resp = await client.post(
        f"/api/rooms/{room_id}/override", json={"target_temp": 70, "duration_hours": -1}
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_thermostat_default_temp_bounds(client) -> None:
    """Verify thermostat default_temp is restricted to 40°F–90°F."""
    # Too high (POST)
    resp = await client.post(
        "/api/thermostats",
        json={"thermostat_entity_id": "climate.test", "default_temp": 95},
    )
    assert resp.status == 400
    assert "target temperature" in (await resp.json())["error"].lower()

    # Valid POST then invalid PUT
    await client.post(
        "/api/thermostats",
        json={"thermostat_entity_id": "climate.test", "default_temp": 70},
    )
    resp = await client.put("/api/thermostats/climate.test", json={"default_temp": 30})
    assert resp.status == 400
