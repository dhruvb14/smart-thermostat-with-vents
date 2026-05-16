"""Short-cycle protection integration tests (Issue #208).

A compressor that stops and restarts within a few minutes is being damaged.
These tests drive the full engine against a fake Home Assistant and verify:

  - off-time lockout — a new cycle cannot start until ``min_cycle_offtime_min``
    has elapsed since the previous cycle ended;
  - minimum runtime — a cycle that reaches target almost immediately is held
    open (HVAC keeps running) until ``min_cycle_runtime_min`` has elapsed,
    rather than completing a too-short cycle;
  - the new config fields round-trip through the REST API and the DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from .fake_ha import SeededEntities

THERMO = "climate.test_thermostat"


async def _create_room_with_schedule(client, target_temp: float = 72.0) -> SeededEntities:
    """Create a room + sensor + vent + an all-day schedule covering "now"."""
    resp = await client.post(
        "/api/rooms",
        json={"name": "Bedroom", "thermostat_entity_id": THERMO},
    )
    room_id = (await resp.json())["id"]

    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.test_room_temp"})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": "cover.test_room_vent", "control_method": "open_close"},
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
            "target_temp": target_temp,
        },
    )
    return SeededEntities(
        thermostat=THERMO,
        sensor="sensor.test_room_temp",
        vent="cover.test_room_vent",
        presence="binary_sensor.test_room_presence",
    )


def _engine(client):
    return client.app["scheduler"]._engines[THERMO]


def _seed_cooling_zone(fake_ha) -> None:
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 78.0, "temperature": 76.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state("sensor.test_room_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.test_room_vent", "closed", {})


@pytest.mark.asyncio
async def test_offtime_lockout_blocks_new_cycle(client, fake_ha, tick) -> None:
    """After a cycle completes, the off-time lockout must defer the next one."""
    _seed_cooling_zone(fake_ha)
    ents = await _create_room_with_schedule(client, target_temp=72.0)

    # 5-minute compressor off-time lockout.
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"min_cycle_offtime_min": 5})
    assert resp.status == 200

    # Tick 1: cooling cycle starts.
    await tick()
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["ended_at"] is None

    # Room reaches target → the cycle completes on the next tick.
    await fake_ha.set_entity_state(ents.sensor, "72.0", {"unit_of_measurement": "°F"})
    await tick()
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is not None, "cycle should have completed"
    assert _engine(client)._last_cycle_ended_at is not None

    # Room is warm again immediately — but the compressor off-time lockout
    # is still active, so no new cycle may start.
    await fake_ha.set_entity_state(ents.sensor, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.reset_calls()
    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1, "off-time lockout must prevent a second cycle from starting"
    assert not fake_ha.calls_for("set_temperature"), "no HVAC command should be sent during lockout"


@pytest.mark.asyncio
async def test_offtime_lockout_releases_after_window(client, fake_ha, tick) -> None:
    """Once the off-time window elapses, a new cycle is allowed to start."""
    _seed_cooling_zone(fake_ha)
    ents = await _create_room_with_schedule(client, target_temp=72.0)
    await client.put(f"/api/thermostats/{THERMO}", json={"min_cycle_offtime_min": 5})

    await tick()  # cycle 1 starts
    await fake_ha.set_entity_state(ents.sensor, "72.0", {"unit_of_measurement": "°F"})
    await tick()  # cycle 1 completes
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["ended_at"] is not None

    # Pretend the previous cycle ended 6 minutes ago — past the lockout.
    _engine(client)._last_cycle_ended_at = datetime.now(UTC) - timedelta(minutes=6)

    await fake_ha.set_entity_state(ents.sensor, "80.0", {"unit_of_measurement": "°F"})
    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 2, "a new cycle should start once the off-time lockout has elapsed"


@pytest.mark.asyncio
async def test_min_runtime_holds_cycle_open(client, fake_ha, tick) -> None:
    """A cycle that reaches target too soon is held open, not completed."""
    _seed_cooling_zone(fake_ha)
    ents = await _create_room_with_schedule(client, target_temp=72.0)
    await client.put(f"/api/thermostats/{THERMO}", json={"min_cycle_runtime_min": 15})

    await tick()  # cycle starts
    eng = _engine(client)
    assert eng.cycle_state.value == "running"

    # Room reaches target almost immediately after the cycle started.
    await fake_ha.set_entity_state(ents.sensor, "72.0", {"unit_of_measurement": "°F"})
    fake_ha.reset_calls()
    await tick()

    # Minimum runtime not met → cycle must stay open and the vent stay open
    # (the HVAC keeps running rather than short-cycling the compressor).
    assert eng.cycle_state.value == "running", "cycle must not complete before min runtime"
    assert not fake_ha.calls_for("close_cover"), "vent must stay open during the min-runtime hold"
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is None

    # Backdate the cycle start past the minimum runtime, then tick again.
    eng._cycle_log.started_at = datetime.now(UTC) - timedelta(minutes=16)
    await tick()

    assert eng.cycle_state.value == "idle", "cycle should complete once min runtime is satisfied"
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is not None


@pytest.mark.asyncio
async def test_short_cycle_config_roundtrips_through_api(client, fake_ha) -> None:
    """The new config fields persist through the REST API and the DB."""
    resp = await client.put(
        f"/api/thermostats/{THERMO}",
        json={"min_cycle_runtime_min": 12, "min_cycle_offtime_min": 7},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["min_cycle_runtime_min"] == 12
    assert body["min_cycle_offtime_min"] == 7

    # Re-read from the DB through the list endpoint.
    listing = await (await client.get("/api/thermostats")).json()
    entry = next(t for t in listing if t["thermostat_entity_id"] == THERMO)
    assert entry["min_cycle_runtime_min"] == 12
    assert entry["min_cycle_offtime_min"] == 7
