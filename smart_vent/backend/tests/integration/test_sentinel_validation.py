"""Reproduction tests for missing input validations."""

import pytest


async def _create_room(client, name="Living Room", thermostat="climate.test"):
    resp = await client.post(
        "/api/rooms",
        json={"name": name, "thermostat_entity_id": thermostat},
    )
    assert resp.status == 201
    return await resp.json()


@pytest.mark.asyncio
async def test_create_room_system_wide_temp_bounds(client):
    # Too high
    resp = await client.post(
        "/api/rooms",
        json={"name": "Room", "thermostat_entity_id": "climate.x", "system_wide_temp": 150},
    )
    assert resp.status == 400
    # Too low
    resp = await client.post(
        "/api/rooms",
        json={"name": "Room", "thermostat_entity_id": "climate.x", "system_wide_temp": 10},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_update_room_system_wide_temp_bounds(client):
    room = await _create_room(client)
    resp = await client.put(f"/api/rooms/{room['id']}", json={"system_wide_temp": 150})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_room_presence_holdover_cap(client):
    resp = await client.post(
        "/api/rooms",
        json={"name": "Room", "thermostat_entity_id": "climate.x", "presence_holdover_hours": 9000},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_create_schedule_target_temp_bounds(client):
    room = await _create_room(client)
    resp = await client.post(
        f"/api/rooms/{room['id']}/schedules",
        json={
            "days_of_week": [0],
            "start_time": "08:00",
            "end_time": "09:00",
            "target_temp": 150.0,
        },
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_update_schedule_target_temp_bounds(client):
    room = await _create_room(client)
    resp = await client.post(
        f"/api/rooms/{room['id']}/schedules",
        json={
            "days_of_week": [0],
            "start_time": "08:00",
            "end_time": "09:00",
            "target_temp": 70.0,
        },
    )
    sched = await resp.json()
    resp = await client.put(
        f"/api/rooms/{room['id']}/schedules/{sched['id']}",
        json={"target_temp": 150.0},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_create_thermostat_default_temp_bounds(client):
    resp = await client.post(
        "/api/thermostats",
        json={"thermostat_entity_id": "climate.t1", "default_temp": 150.0},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_upsert_thermostat_default_temp_bounds(client):
    resp = await client.put(
        "/api/thermostats/climate.t1",
        json={"default_temp": 150.0},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_set_override_bounds(client):
    room = await _create_room(client)
    # Target temp too high
    resp = await client.post(
        f"/api/rooms/{room['id']}/override",
        json={"target_temp": 150.0},
    )
    assert resp.status == 400
    # Duration too long
    resp = await client.post(
        f"/api/rooms/{room['id']}/override",
        json={"target_temp": 70.0, "duration_hours": 9000},
    )
    assert resp.status == 400
