"""Clear Presence must survive a still-occupied room (Issue #439).

Production sequence this pins: the user walks into a room (occupancy sensor
holds "on"), the dashboard's Clear presence deletes the holdover row — and the
continuous-presence refresh (#287) re-upserts a fresh holdover from the
still-"on" sensor on the very next tick. Three clears in a row did nothing;
the room only went idle when the sensors themselves happened to drop.

The fix: clearing writes a presence-suppression marker. While it exists the
refresh sweep and sensor on-edges write no holdover; once every presence
sensor reads off (the room emptied) the sweep deletes the marker and the next
genuine occupancy activates presence exactly as before. The clear endpoint
also ticks the engine immediately so the UI reflects the clear on its next
poll rather than after the next 60 s scheduler tick.
"""

from __future__ import annotations

import pytest

from backend import db

THERMO = "climate.test_thermostat"
PRESENCE = "binary_sensor.room_presence"
SENSOR = "sensor.room_temp"
VENT = "cover.room_vent"


async def _make_presence_room(client, fake_ha) -> str:
    """An occupied, too-warm room whose only demand source is presence."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 78.0, "temperature": 76.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state(SENSOR, "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})
    fake_ha.seed_state(PRESENCE, "on", {})

    resp = await client.post(
        "/api/rooms",
        json={
            "name": "Upstairs Office",
            "thermostat_entity_id": THERMO,
            "presence_holdover_hours": 1.0,
            "system_wide_temp": 72.0,
        },
    )
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": SENSOR})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": VENT, "control_method": "open_close"},
    )
    await client.post(f"/api/rooms/{room_id}/presence", json={"entity_id": PRESENCE})
    return room_id


@pytest.mark.asyncio
async def test_clear_while_occupied_suppresses_instead_of_resurrecting(
    client, fake_ha, tick
) -> None:
    """The #439 production case: clearing presence while the occupancy sensor
    still reads on must stick — the next ticks must NOT re-arm the holdover,
    and the endpoint's immediate engine kick must drop the room from the
    cycle without waiting for the next scheduler tick."""
    room_id = await _make_presence_room(client, fake_ha)

    await tick()  # refresh sweep arms the holdover; presence cycle starts
    eng = client.app["scheduler"]._engines[THERMO]
    assert eng.cycle_state.value == "running"
    assert room_id in eng._active_rooms
    assert eng._active_rooms[room_id].source == "presence"

    resp = await client.delete(f"/api/rooms/{room_id}/presence/holdover")
    assert resp.status == 200

    # The endpoint kicks the engine itself — the room is gone from the cycle
    # BEFORE any scheduler tick (the dashboard's next poll shows the clear).
    assert room_id not in eng._active_rooms, "clear must take effect immediately"

    conn = client.app["scheduler"]._db_conn
    assert await db.get_holdover_state(conn, room_id) is None
    assert await db.is_presence_suppressed(conn, room_id) is True

    # The regression: further ticks with the sensor still on used to re-upsert
    # a fresh holdover and re-add the room within 60 s.
    await tick()
    await tick()
    assert await db.get_holdover_state(conn, room_id) is None, (
        "the continuous-presence refresh must not resurrect a cleared holdover "
        "while the room is still occupied"
    )
    assert room_id not in eng._active_rooms


@pytest.mark.asyncio
async def test_room_emptying_rearms_presence_for_the_next_visit(client, fake_ha, tick) -> None:
    """Once every presence sensor reads off, the suppression re-arms and the
    next occupancy activates presence exactly as before the clear."""
    room_id = await _make_presence_room(client, fake_ha)
    await tick()
    await client.delete(f"/api/rooms/{room_id}/presence/holdover")

    conn = client.app["scheduler"]._db_conn
    assert await db.is_presence_suppressed(conn, room_id) is True

    # The room empties — the sweep on the next tick re-arms presence.
    await fake_ha.set_entity_state(PRESENCE, "off", {})
    await tick()
    assert await db.is_presence_suppressed(conn, room_id) is False, (
        "suppression must re-arm once the room is empty"
    )
    assert await db.get_holdover_state(conn, room_id) is None, (
        "re-arming must not itself create presence demand"
    )

    # Someone enters again — presence behaves exactly as before the clear.
    await fake_ha.set_entity_state(PRESENCE, "on", {})
    await tick()
    holdover = await db.get_holdover_state(conn, room_id)
    assert holdover is not None, "post-re-arm occupancy must arm the holdover again"
    eng = client.app["scheduler"]._engines[THERMO]
    assert room_id in eng._active_rooms
    assert eng._active_rooms[room_id].source == "presence"


@pytest.mark.asyncio
async def test_on_edge_during_suppression_is_ignored(client, fake_ha, tick) -> None:
    """Continuous sensors can emit extra on-events for the same occupancy
    (attribute updates, re-triggers). While suppressed, an on-edge must not
    write a holdover — only an observed empty room re-arms."""
    room_id = await _make_presence_room(client, fake_ha)
    await tick()
    await client.delete(f"/api/rooms/{room_id}/presence/holdover")

    conn = client.app["scheduler"]._db_conn
    # Re-trigger the sensor (off→on within the same tick window counts as the
    # same visit — no tick observed the room empty, so suppression holds).
    await fake_ha.set_entity_state(PRESENCE, "off", {})
    await fake_ha.set_entity_state(PRESENCE, "on", {})

    assert await db.get_holdover_state(conn, room_id) is None, (
        "an on-edge during suppression must not arm a holdover"
    )
    assert await db.is_presence_suppressed(conn, room_id) is True
    await tick()  # sensor reads on again → sweep keeps the suppression
    assert await db.is_presence_suppressed(conn, room_id) is True
    assert await db.get_holdover_state(conn, room_id) is None


@pytest.mark.asyncio
async def test_room_status_reports_suppression(client, fake_ha, tick) -> None:
    """The Rooms page renders the "presence cleared — ignored until the room
    empties" hint from presence_suppressed, so an occupied room with no
    presence demand no longer looks self-contradictory."""
    room_id = await _make_presence_room(client, fake_ha)
    await tick()

    resp = await client.post("/api/rooms/active-status", json={"room_ids": [room_id]})
    status = (await resp.json())[room_id]
    assert status["source"] == "presence"
    assert status["presence_suppressed"] is False

    await client.delete(f"/api/rooms/{room_id}/presence/holdover")

    resp = await client.post("/api/rooms/active-status", json={"room_ids": [room_id]})
    status = (await resp.json())[room_id]
    assert status["source"] == "idle", "no presence demand while suppressed"
    assert status["presence_suppressed"] is True
    assert status["presence_holdover_active"] is False

    # After the room empties and re-arms, the flag drops.
    await fake_ha.set_entity_state(PRESENCE, "off", {})
    await tick()
    resp = await client.post("/api/rooms/active-status", json={"room_ids": [room_id]})
    assert (await resp.json())[room_id]["presence_suppressed"] is False


@pytest.mark.asyncio
async def test_clear_on_unknown_room_is_404(client) -> None:
    resp = await client.delete("/api/rooms/no-such-room/presence/holdover")
    assert resp.status == 404
