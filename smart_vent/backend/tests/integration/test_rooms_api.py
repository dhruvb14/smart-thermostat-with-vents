"""Integration tests for Room CRUD through the HTTP API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend import db as _db
from backend.models import PresenceHoldoverState


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
async def test_clear_presence_holdover_no_holdover(client) -> None:
    resp = await client.post(
        "/api/rooms",
        json={"name": "Den", "thermostat_entity_id": "climate.test_thermostat"},
    )
    room_id = (await resp.json())["id"]

    # Idempotent when no holdover exists
    resp = await client.delete(f"/api/rooms/{room_id}/presence/holdover")
    assert resp.status == 200


@pytest.mark.asyncio
async def test_clear_presence_holdover_active(client) -> None:
    resp = await client.post(
        "/api/rooms",
        json={
            "name": "Den",
            "thermostat_entity_id": "climate.test_thermostat",
            "presence_holdover_hours": 2.0,
        },
    )
    room_id = (await resp.json())["id"]

    # Plant an active holdover directly in the DB
    conn = client.app["scheduler"]._db_conn
    now = datetime.now(UTC)
    state = PresenceHoldoverState(
        room_id=room_id,
        last_detected_at=now,
        expires_at=now + timedelta(hours=2),
    )
    await _db.upsert_holdover_state(conn, state)

    # Confirm it is present via active-status
    status_resp = await client.post("/api/rooms/active-status", json={"room_ids": [room_id]})
    assert (await status_resp.json())[room_id]["presence_holdover_active"] is True

    # Clear it
    resp = await client.delete(f"/api/rooms/{room_id}/presence/holdover")
    assert resp.status == 200

    # Confirm gone
    status_resp = await client.post("/api/rooms/active-status", json={"room_ids": [room_id]})
    assert (await status_resp.json())[room_id]["presence_holdover_active"] is False


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


# ---------------------------------------------------------------------------
# Ambient-aware presence suppression / pre-cool fields (Issue #248, Phase 1)
# ---------------------------------------------------------------------------


OUTDOOR = "sensor.outdoor"


async def _configure_outside_sensor(client, fake_ha, temp_f: float = 75.0) -> None:
    """Register the house-wide outside-temperature entity (Issue #85 Phase 1b).

    Since Issue #524 this is a precondition for turning ambient suppression on,
    so every test that enables the feature has to do it first.
    """
    fake_ha.seed_state(OUTDOOR, str(temp_f), {"unit_of_measurement": "°F"})
    resp = await client.put("/api/settings/outside-temp-entity", json={"entity_id": OUTDOOR})
    assert resp.status == 200, await resp.text()


async def _create_room(client, **extra):
    body = {"name": "Office", "thermostat_entity_id": "climate.test_thermostat", **extra}
    resp = await client.post("/api/rooms", json=body)
    assert resp.status == 201, await resp.text()
    return (await resp.json())["id"]


@pytest.mark.asyncio
async def test_room_ambient_suppression_defaults(client, fake_ha) -> None:
    """A room created without the new fields gets the documented defaults."""
    room_id = await _create_room(client)
    detail = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert detail["ambient_suppression_enabled"] is False
    assert detail["ambient_suppression_mode"] == "any_presence"
    assert detail["ambient_suppression_min_differential"] == 5.0
    assert detail["ambient_suppression_deadband"] == 2.0
    assert detail["ambient_suppression_off_schedule_window_min"] == 60


@pytest.mark.asyncio
async def test_room_ambient_suppression_create_roundtrip(client, fake_ha) -> None:
    await _configure_outside_sensor(client, fake_ha)
    room_id = await _create_room(
        client,
        ambient_suppression_enabled=True,
        ambient_suppression_mode="off_schedule_only",
        ambient_suppression_min_differential=4,
        ambient_suppression_deadband=3,
        ambient_suppression_off_schedule_window_min=90,
    )
    detail = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert detail["ambient_suppression_enabled"] is True
    assert detail["ambient_suppression_mode"] == "off_schedule_only"
    assert detail["ambient_suppression_min_differential"] == 4
    assert detail["ambient_suppression_deadband"] == 3
    assert detail["ambient_suppression_off_schedule_window_min"] == 90


@pytest.mark.asyncio
async def test_room_ambient_suppression_update_roundtrip(client, fake_ha) -> None:
    await _configure_outside_sensor(client, fake_ha)
    room_id = await _create_room(client)
    resp = await client.put(
        f"/api/rooms/{room_id}",
        json={
            "ambient_suppression_enabled": True,
            "ambient_suppression_min_differential": 6,
            "ambient_suppression_deadband": 2.5,
        },
    )
    assert resp.status == 200, await resp.text()
    detail = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert detail["ambient_suppression_enabled"] is True
    assert detail["ambient_suppression_min_differential"] == 6
    assert detail["ambient_suppression_deadband"] == 2.5


@pytest.mark.asyncio
async def test_room_ambient_suppression_celsius_delta_conversion(client, fake_ha) -> None:
    """In Celsius mode the two delta fields are stored as °F (delta, no offset)."""
    client.app["scheduler"]._active_unit = "C"
    try:
        # 2°C differential -> 3.6°F; 2°C widened deadband -> 3.6°F (>= 0.5°F floor).
        room_id = await _create_room(
            client,
            ambient_suppression_min_differential=2,
            ambient_suppression_deadband=2,
        )
    finally:
        client.app["scheduler"]._active_unit = "F"
    conn = client.app["scheduler"]._db_conn
    room = await _db.get_room(conn, room_id)
    assert room is not None
    assert room.ambient_suppression_min_differential == 3.6
    assert room.ambient_suppression_deadband == 3.6


@pytest.mark.asyncio
async def test_room_ambient_suppression_rejects_negative_differential(client, fake_ha) -> None:
    resp = await client.post(
        "/api/rooms",
        json={
            "name": "Bad",
            "thermostat_entity_id": "climate.test_thermostat",
            "ambient_suppression_min_differential": -1,
        },
    )
    assert resp.status == 400
    assert "min_differential" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_room_ambient_suppression_rejects_invalid_mode(client, fake_ha) -> None:
    resp = await client.post(
        "/api/rooms",
        json={
            "name": "Bad",
            "thermostat_entity_id": "climate.test_thermostat",
            "ambient_suppression_mode": "sometimes",
        },
    )
    assert resp.status == 400
    assert "ambient_suppression_mode" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_room_ambient_deadband_must_be_at_least_thermostat_deadband(client, fake_ha) -> None:
    await _configure_outside_sensor(client, fake_ha)
    # Configure the thermostat with a 1.0°F deadband.
    resp = await client.post(
        "/api/thermostats",
        json={
            "thermostat_entity_id": "climate.dband",
            "total_vents_count": 4,
            "deadband": 1.0,
        },
    )
    assert resp.status in (200, 201), await resp.text()

    # Below the thermostat deadband, with the feature enabled -> rejected.
    resp = await client.post(
        "/api/rooms",
        json={
            "name": "Lower",
            "thermostat_entity_id": "climate.dband",
            "ambient_suppression_enabled": True,
            "ambient_suppression_deadband": 0.9,
        },
    )
    assert resp.status == 400
    assert "deadband" in (await resp.json())["error"]

    # Equal to the thermostat deadband -> allowed.
    resp = await client.post(
        "/api/rooms",
        json={
            "name": "Equal",
            "thermostat_entity_id": "climate.dband",
            "ambient_suppression_enabled": True,
            "ambient_suppression_deadband": 1.0,
        },
    )
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["ambient_suppression_deadband"] == 1.0


@pytest.mark.asyncio
async def test_room_ambient_deadband_below_thermostat_allowed_when_disabled(
    client, fake_ha
) -> None:
    """The widened-deadband floor only applies when the feature is enabled, so a
    default widened deadband never blocks a room save on a wide-deadband
    thermostat (regression for the unconditional-rejection edge case)."""
    resp = await client.post(
        "/api/thermostats",
        json={"thermostat_entity_id": "climate.wide", "total_vents_count": 4, "deadband": 3.0},
    )
    assert resp.status in (200, 201), await resp.text()

    # Feature off, widened deadband at the default 2.0 (< the 3.0 thermostat
    # deadband) -> accepted, because the value is unused while disabled.
    resp = await client.post(
        "/api/rooms",
        json={"name": "Wide", "thermostat_entity_id": "climate.wide"},
    )
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["ambient_suppression_deadband"] == 2.0


# ---------------------------------------------------------------------------
# Ambient suppression requires an outside-temperature sensor (Issue #524)
#
# The feature gates entirely on the house-wide outside temperature, so enabling
# it with no sensor configured used to be accepted and then silently do nothing.
# The API now refuses the off → on transition. Only the transition is guarded:
# a room enabled while a sensor existed must stay editable if that sensor is
# later removed, because the Rooms page disables the very checkbox you would
# need to turn the feature back off.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ambient_enable_rejected_on_create_without_outside_sensor(client, fake_ha) -> None:
    resp = await client.post(
        "/api/rooms",
        json={
            "name": "NoSensor",
            "thermostat_entity_id": "climate.test_thermostat",
            "ambient_suppression_enabled": True,
        },
    )
    assert resp.status == 400
    assert "outside-temperature sensor" in (await resp.json())["error"]
    # The room must not have been created by the rejected request.
    assert await (await client.get("/api/rooms")).json() == []


@pytest.mark.asyncio
async def test_ambient_disabled_create_allowed_without_outside_sensor(client, fake_ha) -> None:
    """Only enabling is gated — the other ambient fields still round-trip with
    no sensor, so a room can be pre-configured before the sensor is added."""
    room_id = await _create_room(
        client,
        ambient_suppression_enabled=False,
        ambient_suppression_min_differential=7,
    )
    detail = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert detail["ambient_suppression_enabled"] is False
    assert detail["ambient_suppression_min_differential"] == 7


@pytest.mark.asyncio
async def test_ambient_enable_rejected_on_update_without_outside_sensor(client, fake_ha) -> None:
    room_id = await _create_room(client)
    resp = await client.put(f"/api/rooms/{room_id}", json={"ambient_suppression_enabled": True})
    assert resp.status == 400
    assert "outside-temperature sensor" in (await resp.json())["error"]
    detail = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert detail["ambient_suppression_enabled"] is False


@pytest.mark.asyncio
async def test_ambient_enable_allowed_once_outside_sensor_configured(client, fake_ha) -> None:
    room_id = await _create_room(client)
    resp = await client.put(f"/api/rooms/{room_id}", json={"ambient_suppression_enabled": True})
    assert resp.status == 400

    await _configure_outside_sensor(client, fake_ha)

    resp = await client.put(f"/api/rooms/{room_id}", json={"ambient_suppression_enabled": True})
    assert resp.status == 200, await resp.text()
    detail = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert detail["ambient_suppression_enabled"] is True


@pytest.mark.asyncio
async def test_already_enabled_room_stays_editable_after_sensor_removed(client, fake_ha) -> None:
    """Regression guard for the deadlock the transition-only rule exists to
    avoid: a room enabled with a sensor present must still save (including
    re-sending ambient_suppression_enabled: true, which the Rooms form always
    does) once that sensor is cleared — and must still be able to turn the
    feature off."""
    await _configure_outside_sensor(client, fake_ha)
    room_id = await _create_room(client, ambient_suppression_enabled=True)

    # Clear the house-wide outside sensor.
    resp = await client.put("/api/settings/outside-temp-entity", json={"entity_id": None})
    assert resp.status == 200, await resp.text()

    # An unrelated edit that re-sends the (unchanged) enabled flag still saves.
    resp = await client.put(
        f"/api/rooms/{room_id}",
        json={"name": "Renamed", "ambient_suppression_enabled": True},
    )
    assert resp.status == 200, await resp.text()
    detail = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert detail["name"] == "Renamed"
    assert detail["ambient_suppression_enabled"] is True

    # And the feature can still be turned back off.
    resp = await client.put(f"/api/rooms/{room_id}", json={"ambient_suppression_enabled": False})
    assert resp.status == 200, await resp.text()
    detail = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert detail["ambient_suppression_enabled"] is False

    # Having turned it off, re-enabling is a fresh transition — now blocked.
    resp = await client.put(f"/api/rooms/{room_id}", json={"ambient_suppression_enabled": True})
    assert resp.status == 400
