"""Reproduction tests for missing input validations."""

import pytest


@pytest.fixture
async def celsius_client(fake_ha, db_path):
    """Client where the active temperature unit has been set to Celsius."""
    from aiohttp.test_utils import TestClient, TestServer

    from backend.main import build_app

    app = build_app(fake_ha, db_path, frontend_dist=None, start_ha=False)
    server = TestServer(app)
    async with TestClient(server) as c:
        await c.start_server()
        c.app["scheduler"]._active_unit = "C"
        yield c


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
    assert (await resp.json())["error"] == "system_wide_temp must be between 40.0 and 90.0°F"
    # Too low
    resp = await client.post(
        "/api/rooms",
        json={"name": "Room", "thermostat_entity_id": "climate.x", "system_wide_temp": 10},
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "system_wide_temp must be between 40.0 and 90.0°F"


@pytest.mark.asyncio
async def test_create_room_system_wide_temp_bounds_celsius(celsius_client):
    # Too high: 40°C = 104°F > 90°F
    resp = await celsius_client.post(
        "/api/rooms",
        json={"name": "Room", "thermostat_entity_id": "climate.x", "system_wide_temp": 40},
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "system_wide_temp must be between 4.4 and 32.2°C"


@pytest.mark.asyncio
async def test_update_room_system_wide_temp_bounds(client):
    room = await _create_room(client)
    # Too high
    resp = await client.put(f"/api/rooms/{room['id']}", json={"system_wide_temp": 150})
    assert resp.status == 400
    # Too low
    resp = await client.put(f"/api/rooms/{room['id']}", json={"system_wide_temp": 30})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_room_system_wide_temp_non_numeric(client):
    # Non-numeric in create should return 400, not 500
    resp = await client.post(
        "/api/rooms",
        json={"name": "Room", "thermostat_entity_id": "climate.x", "system_wide_temp": "hot"},
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "system_wide_temp must be numeric"

    room = await _create_room(client, name="Other", thermostat="climate.y")
    resp = await client.put(f"/api/rooms/{room['id']}", json={"system_wide_temp": "warm"})
    assert resp.status == 400
    assert (await resp.json())["error"] == "system_wide_temp must be numeric"


@pytest.mark.asyncio
async def test_room_presence_holdover_cap(client):
    # Too high
    resp = await client.post(
        "/api/rooms",
        json={"name": "Room", "thermostat_entity_id": "climate.x", "presence_holdover_hours": 9000},
    )
    assert resp.status == 400
    # Negative
    resp = await client.post(
        "/api/rooms",
        json={"name": "Room", "thermostat_entity_id": "climate.x", "presence_holdover_hours": -1},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_create_schedule_target_temp_bounds(client):
    room = await _create_room(client)
    # Too high
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
    # Too low
    resp = await client.post(
        f"/api/rooms/{room['id']}/schedules",
        json={
            "days_of_week": [0],
            "start_time": "08:00",
            "end_time": "09:00",
            "target_temp": 30.0,
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
    # Too high
    resp = await client.put(
        f"/api/rooms/{room['id']}/schedules/{sched['id']}",
        json={"target_temp": 150.0},
    )
    assert resp.status == 400
    # Too low
    resp = await client.put(
        f"/api/rooms/{room['id']}/schedules/{sched['id']}",
        json={"target_temp": 30.0},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_update_schedule_target_temp_non_numeric(client):
    room = await _create_room(client)
    resp = await client.post(
        f"/api/rooms/{room['id']}/schedules",
        json={"days_of_week": [0], "start_time": "08:00", "end_time": "09:00", "target_temp": 70.0},
    )
    sched = await resp.json()
    resp = await client.put(
        f"/api/rooms/{room['id']}/schedules/{sched['id']}",
        json={"target_temp": "hot"},
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "target_temp must be numeric"


@pytest.mark.asyncio
async def test_create_thermostat_default_temp_bounds(client):
    # Too high
    resp = await client.post(
        "/api/thermostats",
        json={"thermostat_entity_id": "climate.t1", "default_temp": 150.0},
    )
    assert resp.status == 400
    # Too low
    resp = await client.post(
        "/api/thermostats",
        json={"thermostat_entity_id": "climate.t2", "default_temp": 30.0},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_upsert_thermostat_default_temp_bounds(client):
    # Too high
    resp = await client.put(
        "/api/thermostats/climate.t1",
        json={"default_temp": 150.0},
    )
    assert resp.status == 400
    # Too low
    resp = await client.put(
        "/api/thermostats/climate.t1",
        json={"default_temp": 30.0},
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
    # Target temp too low
    resp = await client.post(
        f"/api/rooms/{room['id']}/override",
        json={"target_temp": 30.0},
    )
    assert resp.status == 400
    # Duration too long
    resp = await client.post(
        f"/api/rooms/{room['id']}/override",
        json={"target_temp": 70.0, "duration_hours": 9000},
    )
    assert resp.status == 400
    # Negative duration
    resp = await client.post(
        f"/api/rooms/{room['id']}/override",
        json={"target_temp": 70.0, "duration_hours": -1},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_set_override_non_numeric(client):
    room = await _create_room(client)
    # Non-numeric target_temp
    resp = await client.post(
        f"/api/rooms/{room['id']}/override",
        json={"target_temp": "warm"},
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "target_temp must be numeric"
    # Non-numeric duration_hours
    resp = await client.post(
        f"/api/rooms/{room['id']}/override",
        json={"target_temp": 70.0, "duration_hours": "forever"},
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "duration_hours must be numeric"
