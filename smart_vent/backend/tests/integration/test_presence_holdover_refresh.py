"""
Regression test for Issue #287: presence holdover must be refreshed while an
occupancy sensor stays continuously "on".

Many presence/occupancy sensors (mmWave, some PIR aggregations) hold a single
continuous `on` state for as long as a room is occupied, emitting no further
state_changed events. Presence was edge-triggered only, so once the initial
holdover elapsed the room went idle even though it was still occupied. The fix
checks current presence-sensor state during the engine tick and refreshes the
holdover for rooms whose sensor currently reads `on`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend import db


@pytest.mark.asyncio
async def test_continuous_presence_arms_holdover_on_tick(client, fake_ha, tick) -> None:
    thermostat = "climate.test_thermostat"
    presence = "binary_sensor.room_presence"

    fake_ha.seed_state(
        thermostat,
        "cool",
        {"current_temperature": 74.0, "temperature": 74.0, "hvac_action": "idle"},
    )
    # Sensor is already "on" before any subscription — no rising-edge event fires.
    fake_ha.seed_state(presence, "on", {})

    resp = await client.post(
        "/api/rooms",
        json={
            "name": "Room",
            "thermostat_entity_id": thermostat,
            "presence_holdover_hours": 2.0,
        },
    )
    room_id = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/presence", json={"entity_id": presence})

    conn = client.app["scheduler"]._db_conn
    # Precondition: no holdover yet (the rising edge never fired).
    assert await db.get_holdover_state(conn, room_id) is None

    await tick()

    holdover = await db.get_holdover_state(conn, room_id)
    assert holdover is not None, "continuous 'on' presence must arm the holdover on tick"
    assert holdover.expires_at > datetime.now(UTC) + timedelta(hours=1)


@pytest.mark.asyncio
async def test_continuous_presence_extends_expiring_holdover(client, fake_ha, tick) -> None:
    thermostat = "climate.test_thermostat"
    presence = "binary_sensor.room_presence"

    fake_ha.seed_state(
        thermostat,
        "cool",
        {"current_temperature": 74.0, "temperature": 74.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state(presence, "on", {})

    resp = await client.post(
        "/api/rooms",
        json={
            "name": "Room",
            "thermostat_entity_id": thermostat,
            "presence_holdover_hours": 2.0,
        },
    )
    room_id = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/presence", json={"entity_id": presence})

    conn = client.app["scheduler"]._db_conn
    # Simulate a holdover about to expire (the room has been occupied for ~2h
    # while the sensor emitted no further events).
    from backend.models import PresenceHoldoverState

    nearly_expired = datetime.now(UTC) + timedelta(seconds=30)
    await db.upsert_holdover_state(
        conn,
        PresenceHoldoverState(
            room_id=room_id,
            last_detected_at=datetime.now(UTC) - timedelta(hours=2),
            expires_at=nearly_expired,
        ),
    )

    await tick()

    holdover = await db.get_holdover_state(conn, room_id)
    assert holdover is not None
    # The still-"on" sensor must have pushed the expiry back out to ~2h.
    assert holdover.expires_at > datetime.now(UTC) + timedelta(hours=1)
