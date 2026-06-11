"""Thermostat availability surface (Issue #267).

Drives the full app against a fake Home Assistant and verifies:

  - ``/api/thermostat-health`` reports thermostats whose climate entity is
    unavailable (or missing from the cache entirely), with the outage age the
    engine has tracked and the per-thermostat abort threshold — this feeds the
    Dashboard banner, mirroring ``/api/sensor-health``;
  - healthy thermostats are omitted;
  - the ``unavailable_abort_after_min`` config field round-trips through the
    REST API and the DB, and rejects invalid values.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"


async def _register_thermostat(client) -> None:
    resp = await client.post(
        "/api/thermostats",
        json={"thermostat_entity_id": THERMO, "name": "Hallway", "total_vents_count": 2},
    )
    assert resp.status == 201, await resp.text()


async def _add_room(client) -> str:
    """Engines only exist for thermostats with rooms — give the zone one so
    the per-tick availability tracking has an engine to live in."""
    resp = await client.post("/api/rooms", json={"name": "Bedroom", "thermostat_entity_id": THERMO})
    assert resp.status == 201, await resp.text()
    room_id: str = (await resp.json())["id"]
    return room_id


@pytest.mark.asyncio
async def test_healthy_thermostat_not_reported(client, fake_ha) -> None:
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 72.0, "temperature": 72.0, "hvac_action": "idle"},
    )
    await _register_thermostat(client)

    health = await (await client.get("/api/thermostat-health")).json()
    assert health["thermostats"] == []


@pytest.mark.asyncio
async def test_unavailable_thermostat_reported_with_outage_age(client, fake_ha, tick) -> None:
    fake_ha.seed_state(THERMO, "unavailable", {})
    await _register_thermostat(client)
    await _add_room(client)

    # One tick lets the engine notice the outage and start its clock.
    await tick()

    health = await (await client.get("/api/thermostat-health")).json()
    assert len(health["thermostats"]) == 1
    entry = health["thermostats"][0]
    assert entry["thermostat_entity_id"] == THERMO
    assert entry["name"] == "Hallway"
    assert entry["reason"] == "unavailable"
    assert entry["abort_after_min"] == 5  # default threshold
    assert entry["unavailable_seconds"] is not None
    assert entry["unavailable_seconds"] >= 0
    assert entry["cycle_running"] is False


@pytest.mark.asyncio
async def test_thermostat_missing_from_cache_reported_as_not_in_cache(client, fake_ha) -> None:
    # Registered in the app but never seen in the HA state cache at all.
    await _register_thermostat(client)

    health = await (await client.get("/api/thermostat-health")).json()
    assert len(health["thermostats"]) == 1
    assert health["thermostats"][0]["reason"] == "not_in_cache"


@pytest.mark.asyncio
async def test_outage_clock_clears_when_thermostat_recovers(client, fake_ha, tick) -> None:
    fake_ha.seed_state(THERMO, "unavailable", {})
    await _register_thermostat(client)
    await _add_room(client)
    await tick()
    health = await (await client.get("/api/thermostat-health")).json()
    assert len(health["thermostats"]) == 1
    assert health["thermostats"][0]["unavailable_seconds"] is not None

    await fake_ha.set_entity_state(
        THERMO,
        "cool",
        {"current_temperature": 72.0, "temperature": 72.0, "hvac_action": "idle"},
    )
    await tick()

    health = await (await client.get("/api/thermostat-health")).json()
    assert health["thermostats"] == []
    engine = client.app["scheduler"].get_engine(THERMO)
    assert engine is not None and engine.unavailable_since is None


@pytest.mark.asyncio
async def test_sustained_outage_aborts_running_cycle_through_full_tick(
    client, fake_ha, tick
) -> None:
    """Full-stack version of the unit test: a cycle started through the real
    tick path is aborted once the outage exceeds the configured threshold."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 78.0, "temperature": 76.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state("sensor.bed", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.bed", "open", {})
    await _register_thermostat(client)
    resp = await client.post("/api/rooms", json={"name": "Bedroom", "thermostat_entity_id": THERMO})
    room_id = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.bed"})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": "cover.bed", "control_method": "open_close"},
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

    await tick()
    engine = client.app["scheduler"].get_engine(THERMO)
    assert engine.cycle_state.value == "running"

    # Thermostat drops off; backdate the outage clock past the 5-min default.
    await fake_ha.set_entity_state(THERMO, "unavailable", {})
    engine._unavailable_since = datetime.now(UTC) - timedelta(minutes=6)
    await tick()

    assert engine.cycle_state.value == "idle"
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_reason"] == "aborted: thermostat unavailable"
    assert fake_ha.get_state("cover.bed")["state"] == "open", "abort must leave the zone vents open"


@pytest.mark.asyncio
async def test_unavailable_abort_config_roundtrips_through_api(client, fake_ha) -> None:
    fake_ha.seed_state(THERMO, "cool", {"current_temperature": 72.0})
    await _register_thermostat(client)

    resp = await client.put(f"/api/thermostats/{THERMO}", json={"unavailable_abort_after_min": 12})
    assert resp.status == 200
    assert (await resp.json())["unavailable_abort_after_min"] == 12

    listing = await (await client.get("/api/thermostats")).json()
    entry = next(t for t in listing if t["thermostat_entity_id"] == THERMO)
    assert entry["unavailable_abort_after_min"] == 12

    # Invalid values are rejected.
    for bad in (-1, "5", 2.5, True):
        resp = await client.put(
            f"/api/thermostats/{THERMO}", json={"unavailable_abort_after_min": bad}
        )
        assert resp.status == 400, f"value {bad!r} should be rejected"
