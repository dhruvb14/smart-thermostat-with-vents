"""
Integration test for Issue #85 Phase 1c: when an outside-temperature HA
entity is configured, the cycle engine reads it via
HAClient.get_numeric_state and persists the value to cycle_logs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


async def _create_room_with_schedule(client) -> str:
    resp = await client.post(
        "/api/rooms",
        json={"name": "Bedroom", "thermostat_entity_id": "climate.test_thermostat"},
    )
    room_id: str = (await resp.json())["id"]
    await client.post(
        f"/api/rooms/{room_id}/sensors",
        json={"entity_id": "sensor.test_room_temp"},
    )
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": "cover.test_room_vent", "control_method": "open_close"},
    )
    now = datetime.now(UTC)
    await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={
            "days_of_week": list(range(7)),
            "start_time": (now - timedelta(hours=1)).time().isoformat(timespec="minutes"),
            "end_time": (now + timedelta(hours=1)).time().isoformat(timespec="minutes"),
            "target_temp": 72.0,
        },
    )
    return room_id


@pytest.mark.asyncio
async def test_outside_temp_persisted_at_cycle_start(client, fake_ha, tick) -> None:
    fake_ha.seed_state(
        "climate.test_thermostat",
        "cool",
        {"current_temperature": 78.0, "temperature": 76.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state("sensor.test_room_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.test_room_vent", "closed", {})
    # 25°C → 77°F via the get_numeric_state conversion.
    fake_ha.seed_state("sensor.outside_c", "25", {"unit_of_measurement": "°C"})

    # Configure the outside-temperature entity through the public API.
    resp = await client.put(
        "/api/settings/outside-temp-entity",
        json={"entity_id": "sensor.outside_c"},
    )
    assert resp.status == 200

    await _create_room_with_schedule(client)
    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1
    # Phase 1c: outside_temp_at_start exposed via the cycle log
    # The /api/logs payload is the existing _cycle_log_to_dict shape, which
    # doesn't yet include outside_temp_at_*. Verify directly through the DB.
    scheduler = client.app["scheduler"]
    conn = await scheduler.get_db()
    async with conn.execute(
        "SELECT outside_temp_at_start, outside_temp_at_end FROM cycle_logs WHERE id=?",
        (logs[0]["id"],),
    ) as cur:
        row = await cur.fetchone()
    assert row["outside_temp_at_start"] == pytest.approx(77.0)
    # Cycle hasn't ended yet → end column is still NULL.
    assert row["outside_temp_at_end"] is None


@pytest.mark.asyncio
async def test_outside_temp_null_when_unset(client, fake_ha, tick) -> None:
    # No outside-temp entity configured — the cycle log columns must stay NULL.
    fake_ha.seed_state(
        "climate.test_thermostat",
        "cool",
        {"current_temperature": 78.0, "temperature": 76.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state("sensor.test_room_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.test_room_vent", "closed", {})

    await _create_room_with_schedule(client)
    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1
    scheduler = client.app["scheduler"]
    conn = await scheduler.get_db()
    async with conn.execute(
        "SELECT outside_temp_at_start FROM cycle_logs WHERE id=?",
        (logs[0]["id"],),
    ) as cur:
        row = await cur.fetchone()
    assert row["outside_temp_at_start"] is None
