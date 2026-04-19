"""Integration tests for Room CRUD through the HTTP API."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_list_get_delete_room(client, fake_ha) -> None:
    # Empty to start
    resp = await client.get("/api/rooms")
    assert await resp.json() == []

    # Create
    resp = await client.post(
        "/api/rooms",
        json={"name": "Bedroom", "thermostat_entity_id": "climate.test_thermostat"},
    )
    assert resp.status == 201
    created = await resp.json()
    room_id = created["id"]

    # List
    resp = await client.get("/api/rooms")
    rooms = await resp.json()
    assert len(rooms) == 1
    assert rooms[0]["name"] == "Bedroom"

    # Get (detailed)
    resp = await client.get(f"/api/rooms/{room_id}")
    detail = await resp.json()
    assert detail["name"] == "Bedroom"
    assert detail["sensors"] == []
    assert detail["vents"] == []

    # Pure CRUD should never write to HA
    assert fake_ha.calls == []

    # Delete
    resp = await client.delete(f"/api/rooms/{room_id}")
    assert resp.status == 200

    resp = await client.get("/api/rooms")
    assert await resp.json() == []


@pytest.mark.asyncio
async def test_add_sensor_and_vent_to_room(client, fake_ha) -> None:
    resp = await client.post(
        "/api/rooms",
        json={"name": "Office", "thermostat_entity_id": "climate.test_thermostat"},
    )
    room_id = (await resp.json())["id"]

    resp = await client.post(
        f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.office_temp"}
    )
    assert resp.status == 201

    resp = await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": "cover.office_vent", "control_method": "open_close"},
    )
    assert resp.status == 201

    detail = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert [s["entity_id"] for s in detail["sensors"]] == ["sensor.office_temp"]
    assert [v["entity_id"] for v in detail["vents"]] == ["cover.office_vent"]

    # Still no HA writes — only DB mutations.
    assert fake_ha.calls == []


@pytest.mark.asyncio
async def test_system_enable_disable_roundtrip(client) -> None:
    resp = await client.get("/api/system/status")
    status = await resp.json()
    assert status["enabled"] is True
    assert status["dev_mode"] is False

    resp = await client.post("/api/system/enabled", json={"enabled": False})
    assert (await resp.json())["enabled"] is False

    resp = await client.get("/api/system/status")
    assert (await resp.json())["enabled"] is False
