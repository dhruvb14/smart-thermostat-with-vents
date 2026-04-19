"""End-to-end cycle flow against a fake Home Assistant.

Exercises schedule → cycle start → vent open + setpoint write →
sensor reaches target → vent close → cycle log closed with diagnostics.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from .fake_ha import SeededEntities


async def _create_room_with_schedule(
    client, target_temp: float = 72.0
) -> tuple[str, SeededEntities]:
    """Create a room + sensor + vent + schedule covering "now" via the API."""
    resp = await client.post(
        "/api/rooms",
        json={"name": "Bedroom", "thermostat_entity_id": "climate.test_thermostat"},
    )
    room_id = (await resp.json())["id"]

    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.test_room_temp"})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": "cover.test_room_vent", "control_method": "open_close"},
    )

    # Schedule covering now ± 1h every day
    now = datetime.now()
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
    return room_id, SeededEntities(
        thermostat="climate.test_thermostat",
        sensor="sensor.test_room_temp",
        vent="cover.test_room_vent",
        presence="binary_sensor.test_room_presence",
    )


@pytest.mark.asyncio
async def test_schedule_driven_cycle_starts_vents_open_and_setpoint_written(
    client, fake_ha, tick
) -> None:
    # Seed HA: thermostat in cool, room is warm → cooling cycle expected.
    fake_ha.seed_state(
        "climate.test_thermostat",
        "cool",
        {
            "current_temperature": 78.0,
            "temperature": 76.0,
            "hvac_action": "idle",
        },
    )
    fake_ha.seed_state("sensor.test_room_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.test_room_vent", "closed", {})

    _room_id, ents = await _create_room_with_schedule(client, target_temp=72.0)

    await tick()

    # Engine should have opened the vent and written a setpoint.
    open_calls = fake_ha.calls_for("open_cover")
    assert len(open_calls) >= 1
    assert open_calls[0].data["entity_id"] == ents.vent

    setpoint_calls = fake_ha.calls_for("set_temperature")
    assert len(setpoint_calls) >= 1
    # In cooling mode the setpoint should be at or below the target + deadband
    assert setpoint_calls[-1].data["entity_id"] == ents.thermostat
    assert setpoint_calls[-1].data["temperature"] <= 72.0

    # A cycle log row should exist and be open (ended_at is null).
    resp = await client.get("/api/logs")
    logs = await resp.json()
    assert len(logs) == 1
    assert logs[0]["ended_at"] is None
    assert logs[0]["mode"] == "cooling"


@pytest.mark.asyncio
async def test_reach_target_closes_vent_and_records_diagnostic(client, fake_ha, tick) -> None:
    fake_ha.seed_state(
        "climate.test_thermostat",
        "cool",
        {
            "current_temperature": 78.0,
            "temperature": 76.0,
            "hvac_action": "cooling",
        },
    )
    fake_ha.seed_state("sensor.test_room_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.test_room_vent", "closed", {})

    _room_id, ents = await _create_room_with_schedule(client, target_temp=72.0)

    # First tick: cycle starts, vent opens.
    await tick()
    assert fake_ha.calls_for("open_cover"), "cycle should have opened the vent"

    logs = await (await client.get("/api/logs")).json()
    cycle_id = logs[0]["id"]

    # Now the room hits target — bump sensor to 72.0 and tick again.
    await fake_ha.set_entity_state(ents.sensor, "72.0", {"unit_of_measurement": "°F"})
    fake_ha.reset_calls()
    await tick()

    close_calls = fake_ha.calls_for("close_cover")
    assert close_calls, f"vent should have closed after reaching target; calls={fake_ha.calls}"
    assert close_calls[0].data["entity_id"] == ents.vent

    # Diagnostic row: closed_reached_target vent event on the new #60 table.
    resp = await client.get(f"/api/logs/{cycle_id}/detail")
    detail = await resp.json()
    actions = [e["action"] for e in detail["vent_events"]]
    assert "closed_reached_target" in actions


@pytest.mark.asyncio
async def test_dev_mode_runs_engine_but_writes_no_ha_calls(client, fake_ha, tick) -> None:
    fake_ha.seed_state(
        "climate.test_thermostat",
        "cool",
        {
            "current_temperature": 78.0,
            "temperature": 76.0,
            "hvac_action": "idle",
        },
    )
    fake_ha.seed_state("sensor.test_room_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.test_room_vent", "closed", {})

    await _create_room_with_schedule(client, target_temp=72.0)

    # Turn on dev mode via the API — this flips the same flag production uses.
    resp = await client.post("/api/system/dev-mode", json={"dev_mode": True})
    assert (await resp.json())["dev_mode"] is True

    await tick()

    # Dev mode intercepts every write in ha_client / FakeHomeAssistant: no
    # service calls should have been recorded.
    assert fake_ha.calls == [], f"unexpected HA calls in dev mode: {fake_ha.calls}"

    # But the engine still ran — a cycle log should exist in the DB.
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1


@pytest.mark.asyncio
async def test_system_disable_aborts_running_cycle(client, fake_ha, tick) -> None:
    fake_ha.seed_state(
        "climate.test_thermostat",
        "cool",
        {
            "current_temperature": 78.0,
            "temperature": 76.0,
            "hvac_action": "cooling",
        },
    )
    fake_ha.seed_state("sensor.test_room_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.test_room_vent", "closed", {})

    await _create_room_with_schedule(client, target_temp=72.0)

    await tick()
    logs = await (await client.get("/api/logs")).json()
    assert logs and logs[0]["ended_at"] is None
    cycle_id = logs[0]["id"]

    # Disable the system — scheduler calls force_abort on every engine.
    resp = await client.post("/api/system/enabled", json={"enabled": False})
    assert resp.status == 200

    logs = await (await client.get("/api/logs")).json()
    row = next(log for log in logs if log["id"] == cycle_id)
    assert row["ended_at"] is not None
    assert (row.get("ended_reason") or "").startswith("aborted:")


@pytest.mark.asyncio
async def test_fake_ha_mirrors_real_client_public_api(fake_ha) -> None:
    """Guard: the fake must expose every public method the real client has.

    If the real ``HAClient`` gains a method, this test fails so we notice.
    """
    from backend.ha_client import HAClient

    real_public = {
        name
        for name in vars(HAClient)
        if not name.startswith("_") and callable(getattr(HAClient, name))
    }
    fake_public = {
        name
        for name in dir(fake_ha)
        if not name.startswith("_") and callable(getattr(fake_ha, name))
    }
    missing = real_public - fake_public
    assert not missing, f"FakeHomeAssistant missing public methods: {missing}"


@pytest.mark.asyncio
async def test_cycle_start_respects_deadband(client, fake_ha, tick) -> None:
    """A cycle only starts if room temp exceeds target +/- deadband.

    If target is 72.0 and deadband is 1.0, a temp of 72.5 does NOT start a cycle,
    but a temp of 73.1 does.
    """
    _, entities = await _create_room_with_schedule(client, target_temp=72.0)

    # Explicitly set deadband to 1.0 for the thermostat
    await client.put(
        f"/api/thermostats/{entities.thermostat}/config",
        json={"overshoot_delta": 2.0, "deadband": 1.0, "min_open_vents": 1},
    )

    # 1. Inside deadband (72.5 <= 72.0 + 1.0) -> No cycle expected
    fake_ha.seed_state(
        entities.thermostat,
        "cool",
        {"current_temperature": 75.0, "temperature": 75.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state(entities.sensor, "72.5", {"unit_of_measurement": "°F"})

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 0, "No cycle should start when room is within deadband"
    calls = fake_ha.calls_for("set_temperature")
    assert len(calls) == 1, "Thermostat is reset to ambient when idle"
    assert calls[0].data["temperature"] == 75.0, (
        "Thermostat should be set to ambient to keep it off"
    )
    fake_ha.reset_calls()

    # 2. Outside deadband (73.1 > 72.0 + 1.0) -> Cycle starts
    await fake_ha.set_entity_state(entities.sensor, "73.1", {"unit_of_measurement": "°F"})

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1, "Cycle should start when room temp exceeds deadband"
    assert logs[0]["ended_at"] is None
    assert len(fake_ha.calls_for("set_temperature")) > 0, "Thermostat should be activated"
