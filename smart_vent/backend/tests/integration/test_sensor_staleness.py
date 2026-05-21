"""Sensor-staleness guard integration test (Issue #211).

A battery sensor that drops off the mesh keeps its last numeric state in HA.
If the engine averages that stale value into the room temperature, it can
silently make the *wrong* control decision — the worst kind of failure mode
because nothing surfaces as broken. This test pins the end-to-end behaviour:
a stale reading at a wrong-direction value does NOT poison the room average,
the cycle is decided from the fresh sensor alone, and a warning event is
written naming the offending sensor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"


async def _create_room_with_two_sensors(client) -> str:
    """Cooling scenario: warm room on an all-day schedule, target 72°F."""
    resp = await client.post("/api/rooms", json={"name": "Bedroom", "thermostat_entity_id": THERMO})
    room_id: str = (await resp.json())["id"]
    for eid in ("sensor.fresh_temp", "sensor.dead_battery"):
        await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": eid})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": "cover.bedroom_vent", "control_method": "open_close"},
    )
    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).time().replace(second=0, microsecond=0)
    end = (now + timedelta(hours=1)).time().replace(second=0, microsecond=0)
    await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={
            "days_of_week": list(range(7)),
            "start_time": start.isoformat(timespec="minutes"),
            "end_time": end.isoformat(timespec="minutes"),
            "target_temp": 72.0,
        },
    )
    return room_id


@pytest.mark.asyncio
async def test_stale_sensor_does_not_poison_room_average(client, fake_ha, tick) -> None:
    """The fresh sensor says the room is warm (78°F) and needs cooling. The
    stale sensor would inject 60°F into the average and invert the decision —
    if the engine trusted it. The guard must keep that from happening."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 78.0, "temperature": 76.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state("sensor.fresh_temp", "78.0", {"unit_of_measurement": "°F"})
    # A 3-hour-old reading at a wrong-direction value: without the guard, the
    # average would be (78 + 60) / 2 = 69°F → engine infers "off", no cycle.
    fake_ha.seed_state(
        "sensor.dead_battery",
        "60.0",
        {"unit_of_measurement": "°F"},
        last_updated=(datetime.now(UTC) - timedelta(hours=3)).isoformat(),
    )
    fake_ha.seed_state("cover.bedroom_vent", "open", {})
    await _create_room_with_two_sensors(client)

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1, "cooling cycle must start from the fresh reading alone"
    assert logs[0]["mode"] == "cooling"

    # And the dead sensor is surfaced in the event log so it can be fixed.
    events = await (await client.get("/api/logs/events?level=warning")).json()
    messages = [e["message"] for e in events]
    assert any("sensor.dead_battery" in m and "minutes" in m for m in messages), messages


@pytest.mark.asyncio
async def test_all_room_sensors_stale_falls_back_to_thermostat_ambient(
    client, fake_ha, tick
) -> None:
    """When every room sensor is stale, _get_avg_temp returns None and
    _infer_mode falls through to the thermostat's own ambient reading — the
    cycle is still decided correctly rather than going dark."""
    # Thermostat ambient 78°F (warm) — its own sensor still reports.
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 78.0, "temperature": 76.0, "hvac_action": "idle"},
    )
    stale_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    fake_ha.seed_state(
        "sensor.fresh_temp", "70.0", {"unit_of_measurement": "°F"}, last_updated=stale_ts
    )
    fake_ha.seed_state(
        "sensor.dead_battery", "70.0", {"unit_of_measurement": "°F"}, last_updated=stale_ts
    )
    fake_ha.seed_state("cover.bedroom_vent", "open", {})
    await _create_room_with_two_sensors(client)

    await tick()

    # The fallback path uses the thermostat's 78°F vs target 72°F → still
    # infers cooling. The cycle starts; the stale sensors did not block it.
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1
    assert logs[0]["mode"] == "cooling"


@pytest.mark.asyncio
async def test_sensor_staleness_setting_roundtrip(client) -> None:
    """The threshold defaults to 30 min and persists across GET/PUT cycles
    so the Settings page can configure it. Invalid values are rejected."""
    resp = await client.get("/api/settings/sensor-staleness")
    assert resp.status == 200
    assert (await resp.json())["stale_after_min"] == 30.0

    resp = await client.put("/api/settings/sensor-staleness", json={"stale_after_min": 45})
    assert resp.status == 200
    assert (await resp.json())["stale_after_min"] == 45.0

    # Persists on next GET.
    assert (await (await client.get("/api/settings/sensor-staleness")).json())[
        "stale_after_min"
    ] == 45.0

    # Out of range — rejected.
    resp = await client.put("/api/settings/sensor-staleness", json={"stale_after_min": 0})
    assert resp.status == 400
    resp = await client.put("/api/settings/sensor-staleness", json={"stale_after_min": 24 * 60 + 1})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_setting_change_takes_effect_on_next_tick(client, fake_ha, tick) -> None:
    """Raising the threshold past a sensor's age must un-stale it on the next
    tick — proving the engine reads the setting live rather than caching the
    constant at startup."""
    # Seed a room sensor that is 45 minutes old. Default threshold is 30 →
    # the engine treats it as stale and the warning we already covered fires.
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 78.0, "temperature": 76.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state(
        "sensor.fresh_temp",
        "78.0",
        {"unit_of_measurement": "°F"},
        last_updated=(datetime.now(UTC) - timedelta(minutes=45)).isoformat(),
    )
    fake_ha.seed_state("sensor.dead_battery", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.bedroom_vent", "open", {})
    await _create_room_with_two_sensors(client)

    # Raise the threshold to 60 minutes — 45-min reading is now fresh.
    await client.put("/api/settings/sensor-staleness", json={"stale_after_min": 60})
    await tick()

    # Sensor-health reports zero stale sensors with the new threshold.
    health = await (await client.get("/api/sensor-health")).json()
    assert health["stale_after_min"] == 60.0
    assert health["rooms"] == []


@pytest.mark.asyncio
async def test_sensor_health_lists_stale_sensors(client, fake_ha, tick) -> None:
    """/api/sensor-health drives the Dashboard banner and the Room badges.
    It must list every configured room sensor whose age exceeds the
    threshold, naming both the room and the entity."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 78.0, "temperature": 76.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state("sensor.fresh_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(
        "sensor.dead_battery",
        "60.0",
        {"unit_of_measurement": "°F"},
        last_updated=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    )
    fake_ha.seed_state("cover.bedroom_vent", "open", {})
    await _create_room_with_two_sensors(client)

    health = await (await client.get("/api/sensor-health")).json()
    assert health["stale_after_min"] == 30.0
    assert len(health["rooms"]) == 1
    room = health["rooms"][0]
    assert room["room_name"] == "Bedroom"

    stale_ids = {s["entity_id"] for s in room["stale_sensors"]}
    assert stale_ids == {"sensor.dead_battery"}
    dead = next(s for s in room["stale_sensors"] if s["entity_id"] == "sensor.dead_battery")
    # Roughly 2 hours, with execution slack.
    assert 2 * 3600 - 60 < dead["age_seconds"] < 2 * 3600 + 60
    assert dead["reason"] == "stale"
