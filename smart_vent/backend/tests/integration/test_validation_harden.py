
import pytest


@pytest.mark.asyncio
async def test_room_validation_harden(client):
    # Test holdover limit
    payload = {
        "name": "Test Room",
        "thermostat_entity_id": "climate.test",
        "presence_holdover_hours": 9000
    }
    resp = await client.post("/api/rooms", json=payload)
    assert resp.status == 400
    data = await resp.json()
    assert "exceeds maximum allowed" in data["error"]

    # Test system_wide_temp bounds
    payload["presence_holdover_hours"] = 2.0
    payload["system_wide_temp"] = 100
    resp = await client.post("/api/rooms", json=payload)
    assert resp.status == 400
    data = await resp.json()
    assert "between 40 and 90°F" in data["error"]

@pytest.mark.asyncio
async def test_schedule_validation_harden(client):
    # Create a room first
    resp = await client.post("/api/rooms", json={"name": "R1", "thermostat_entity_id": "climate.t1"})
    room = await resp.json()
    room_id = room["id"]

    # Test target_temp bounds
    payload = {
        "days_of_week": [0],
        "start_time": "08:00:00",
        "end_time": "10:00:00",
        "target_temp": 30
    }
    resp = await client.post(f"/api/rooms/{room_id}/schedules", json=payload)
    assert resp.status == 400
    data = await resp.json()
    assert "between 40 and 90°F" in data["error"]

@pytest.mark.asyncio
async def test_override_validation_harden(client):
    # Create a room first
    resp = await client.post("/api/rooms", json={"name": "R1", "thermostat_entity_id": "climate.t1"})
    room = await resp.json()
    room_id = room["id"]

    # Test target_temp bounds
    payload = {"target_temp": 95, "duration_hours": 1}
    resp = await client.post(f"/api/rooms/{room_id}/override", json=payload)
    assert resp.status == 400
    data = await resp.json()
    assert "between 40 and 90°F" in data["error"]

    # Test duration limit
    payload = {"target_temp": 70, "duration_hours": 10000}
    resp = await client.post(f"/api/rooms/{room_id}/override", json=payload)
    assert resp.status == 400
    data = await resp.json()
    assert "exceeds maximum allowed" in data["error"]
