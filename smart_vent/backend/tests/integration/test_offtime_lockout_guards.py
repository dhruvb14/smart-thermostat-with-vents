"""Compressor off-time lockout coverage for the direct-command paths (#426).

The #208 off-time lockout gated only the IDLE→RUNNING cycle path. Two newer
paths command the compressor directly and bypassed it:

  - the #367 safety backstop (`_enforce_safety_setpoint`), reachable the very
    tick after a cooling cycle ends when demand disappears but ambient still
    breaches ``max_setpoint``;
  - the vacation hold, which is applied in the SAME tick that vacation-mode
    activation aborts a running cycle — a compressor stop→start within
    seconds.

Both now defer cooling while the lockout runs (re-evaluated every tick, so
the command fires the moment it elapses). Heating stays exempt — the lockout
protects the compressor, and heat is furnace-side.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"
SENSOR = "sensor.room_temp"
VENT = "cover.room_vent"


def _engine(client):
    return client.app["scheduler"]._engines[THERMO]


def _cool_commands(fake_ha) -> list:
    return [
        c
        for c in fake_ha.calls_for("set_temperature")
        if c.data["entity_id"] == THERMO and c.data.get("hvac_mode") == "cool"
    ]


async def _make_room(client, *, schedule_target: float | None = None) -> str:
    resp = await client.post("/api/rooms", json={"name": "Room", "thermostat_entity_id": THERMO})
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": SENSOR})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": VENT, "control_method": "open_close"},
    )
    if schedule_target is not None:
        now = datetime.now(UTC)
        start = (now - timedelta(hours=1)).time().replace(second=0, microsecond=0)
        end = (now + timedelta(hours=1)).time().replace(second=0, microsecond=0)
        await client.post(
            f"/api/rooms/{room_id}/schedules",
            json={
                "days_of_week": list(range(7)),
                "start_time": start.isoformat(timespec="minutes"),
                "end_time": end.isoformat(timespec="minutes"),
                "target_temp": schedule_target,
            },
        )
    return room_id


@pytest.mark.asyncio
async def test_safety_backstop_defers_cooling_during_lockout(client, fake_ha, tick) -> None:
    """Ambient breaches max_setpoint with no room demand right after a cycle
    ended: the backstop must wait out the off-time lockout, then command."""
    # Thermostat ambient 80 breaches max_setpoint 78; the room's own sensor is
    # inside the envelope so the per-room safety path stays out of the way.
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 80.0, "temperature": 82.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state(SENSOR, "72.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})
    await _make_room(client)  # no schedule → zero active rooms → backstop path
    await client.put(
        f"/api/thermostats/{THERMO}",
        json={"max_setpoint": 78.0, "min_setpoint": 65.0, "min_cycle_offtime_min": 5},
    )

    eng = _engine(client)
    # A cooling cycle just ended — the lockout clock is running.
    eng._last_cycle_ended_at = datetime.now(UTC)

    fake_ha.reset_calls()
    await tick()
    assert not _cool_commands(fake_ha), (
        "safety backstop must not restart the compressor inside the off-time lockout"
    )
    events = await (await client.get("/api/logs/events?limit=20")).json()
    assert any("deferred" in e["message"] and "lockout" in e["message"] for e in events), (
        "the deferral must be visible in the event log"
    )

    # Lockout elapses → the very next tick commands the bound.
    eng._last_cycle_ended_at = datetime.now(UTC) - timedelta(minutes=6)
    await tick()
    cools = _cool_commands(fake_ha)
    assert cools, "backstop must fire once the lockout has elapsed"
    assert cools[-1].data["temperature"] == pytest.approx(78.0)


@pytest.mark.asyncio
async def test_safety_backstop_heating_is_exempt_from_lockout(client, fake_ha, tick) -> None:
    """The lockout protects the compressor; a heat-side envelope breach is
    commanded immediately even during the off-time window."""
    fake_ha.seed_state(
        THERMO,
        "heat",
        {"current_temperature": 60.0, "temperature": 58.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state(SENSOR, "70.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})
    await _make_room(client)
    await client.put(
        f"/api/thermostats/{THERMO}",
        json={"max_setpoint": 78.0, "min_setpoint": 65.0, "min_cycle_offtime_min": 5},
    )

    _engine(client)._last_cycle_ended_at = datetime.now(UTC)
    fake_ha.reset_calls()
    await tick()

    heats = [
        c
        for c in fake_ha.calls_for("set_temperature")
        if c.data["entity_id"] == THERMO and c.data.get("hvac_mode") == "heat"
    ]
    assert heats, "heating backstop must not be deferred by the compressor lockout"
    assert heats[-1].data["temperature"] == pytest.approx(65.0)


@pytest.mark.asyncio
async def test_vacation_hold_defers_cooling_after_aborting_a_cycle(client, fake_ha, tick) -> None:
    """Turning vacation on mid-cooling-cycle aborts the cycle (compressor
    stops); the single-setpoint hold must not restart it in the same breath."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 80.0, "temperature": 78.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state(SENSOR, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})
    await _make_room(client, schedule_target=72.0)
    await client.put(
        f"/api/thermostats/{THERMO}",
        json={
            "max_setpoint": 78.0,
            "min_setpoint": 65.0,
            "min_cycle_offtime_min": 5,
            "vacation_hvac_mode": "single",
        },
    )

    await tick()  # cooling cycle starts
    eng = _engine(client)
    assert eng.cycle_state.value == "running"

    fake_ha.reset_calls()
    return_at = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    resp = await client.post("/api/settings/vacation-mode", json={"return_at": return_at})
    assert resp.status == 200
    await tick()

    assert eng.cycle_state.value == "idle", "vacation activation must abort the cycle"
    # Ambient 80 > max_setpoint 78, but the abort just armed the lockout: the
    # hold must NOT command cool @ 78 yet. (The abort's own parked-setpoint
    # write is fine — it stops the HVAC; we assert no bound-hold command.)
    hold_cools = [c for c in _cool_commands(fake_ha) if c.data["temperature"] == 78.0]
    assert not hold_cools, "vacation hold must not stop→start the compressor within the lockout"

    # Lockout elapses → the hold commands the bound.
    eng._last_cycle_ended_at = datetime.now(UTC) - timedelta(minutes=6)
    await tick()
    hold_cools = [c for c in _cool_commands(fake_ha) if c.data["temperature"] == 78.0]
    assert hold_cools, "vacation hold must cool to max_setpoint once the lockout elapses"
