"""
🛡️ Sentinel: Input validation bounds tests.
Verify that all API endpoints enforce safe bounds for temperatures and durations.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_room_system_wide_temp_bounds(client) -> None:
    """Verify system_wide_temp is restricted to [40, 90]°F."""
    # Too low (30°F)
    resp = await client.post(
        "/api/rooms",
        json={"name": "Cold Room", "thermostat_entity_id": "climate.test", "system_wide_temp": 30},
    )
    assert resp.status == 400
    assert "between 40 and 90" in (await resp.json())["error"]

    # Too high (100°F)
    resp = await client.post(
        "/api/rooms",
        json={"name": "Hot Room", "thermostat_entity_id": "climate.test", "system_wide_temp": 100},
    )
    assert resp.status == 400
    assert "between 40 and 90" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_override_bounds(client) -> None:
    """Verify set_override validates target_temp and duration_hours."""
    # Create a room first
    resp = await client.post(
        "/api/rooms", json={"name": "Test", "thermostat_entity_id": "climate.test"}
    )
    room_id = (await resp.json())["id"]

    # Invalid temp
    resp = await client.post(f"/api/rooms/{room_id}/override", json={"target_temp": 110})
    assert resp.status == 400

    # Negative duration
    resp = await client.post(
        f"/api/rooms/{room_id}/override", json={"target_temp": 70, "duration_hours": -1}
    )
    assert resp.status == 400

    # Excessive duration
    resp = await client.post(
        f"/api/rooms/{room_id}/override", json={"target_temp": 70, "duration_hours": 99999}
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_schedule_temp_bounds(client) -> None:
    """Verify schedule target_temp is restricted to [40, 90]°F."""
    resp = await client.post(
        "/api/rooms", json={"name": "Test", "thermostat_entity_id": "climate.test"}
    )
    room_id = (await resp.json())["id"]

    resp = await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={"days_of_week": [0], "start_time": "08:00", "end_time": "09:00", "target_temp": 120},
    )
    assert resp.status == 400
