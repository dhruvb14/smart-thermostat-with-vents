"""End-to-end test for the cycle-timeout safety guard (Issue #212).

``cycle_timeout_hours`` is the engine's runaway-equipment interlock: a cycle
that has run longer than the configured limit is terminated regardless of
whether any room has reached its target.  Without it a stuck damper, failed
sensor, or undersized HVAC could keep the compressor or furnace running
indefinitely against a target it will never satisfy.

Before this file, the only coverage of the guard was a ``_terminate_cycle``
unit test in ``test_cycle_diagnostics.py`` that called the method directly
with ``reason='timeout'`` — it never actually advanced the elapsed-time
clock or proved that the engine itself would fire the termination. These
integration tests drive the engine through the aiohttp app and a fake HA,
fast-forward ``cycle_log.started_at`` to a time in the past, and assert the
full chain: the cycle closes with ``ended_reason='timeout'``, a warning is
written to the event log, and the thermostat setpoint is reset to ambient
so the HVAC shuts off.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"


async def _create_heating_room(client) -> str:
    """A cold room on an all-day heating schedule — the engine will start a cycle."""
    resp = await client.post("/api/rooms", json={"name": "Bedroom", "thermostat_entity_id": THERMO})
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.bedroom_temp"})
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
            "target_temp": 68.0,
        },
    )
    return room_id


def _seed_cold_room(fake_ha) -> None:
    fake_ha.seed_state(
        THERMO,
        "heat",
        {"current_temperature": 65.0, "temperature": 65.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state("sensor.bedroom_temp", "60.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.bedroom_vent", "closed", {})


def _engine(client):
    return client.app["scheduler"]._engines[THERMO]


async def _warning_messages(client) -> list[str]:
    events = await (await client.get("/api/logs/events?level=warning")).json()
    return [e["message"] for e in events]


@pytest.mark.asyncio
async def test_cycle_terminates_after_timeout(client, fake_ha, tick) -> None:
    """A cycle past ``cycle_timeout_hours`` is closed with reason='timeout', a
    warning event is logged, and the thermostat setpoint is reset to ambient
    so the HVAC shuts off."""
    _seed_cold_room(fake_ha)
    await _create_heating_room(client)
    # Default cycle_timeout_hours = 3.0.  No PUT needed.
    await tick()
    started = await (await client.get("/api/logs")).json()
    assert len(started) == 1, "cycle should have started"
    cycle_id = started[0]["id"]

    # Fast-forward the cycle's start time so the next tick sees it as expired.
    engine = _engine(client)
    assert engine.cycle_state.value == "running"
    engine._cycle_log.started_at = datetime.now(UTC) - timedelta(hours=3, minutes=10)

    fake_ha.reset_calls()  # discard the start-of-cycle setpoint write
    await tick()

    closed = next(c for c in await (await client.get("/api/logs")).json() if c["id"] == cycle_id)
    assert closed["ended_at"] is not None, "cycle must be closed"
    assert closed["ended_reason"] == "timeout", closed["ended_reason"]
    assert engine.cycle_state.value == "idle"

    # The warning event is what makes a stuck cycle visible in the UI.
    warnings = await _warning_messages(client)
    assert any("timed out" in m for m in warnings), warnings

    # Setpoint parked at ambient 65 − overshoot 2 = 63°F (heating cycle) so
    # the HVAC stops — the whole point of this guard — and stays stopped
    # until the zone genuinely drops a full overshoot below where it is now.
    sp_calls = fake_ha.calls_for("set_temperature")
    assert sp_calls, "engine should write a setpoint when terminating"
    last_setpoint = sp_calls[-1].data["temperature"]
    assert last_setpoint == pytest.approx(63.0, abs=0.5), (
        f"last setpoint {last_setpoint} should be parked at ambient 65 − overshoot 2"
    )


@pytest.mark.asyncio
async def test_cycle_just_under_timeout_is_not_terminated(client, fake_ha, tick) -> None:
    """A cycle that has run less than ``cycle_timeout_hours`` keeps running —
    the elapsed-time check is a strict ``>`` so the boundary is not aborted."""
    _seed_cold_room(fake_ha)
    await _create_heating_room(client)
    await tick()
    cycle_id = (await (await client.get("/api/logs")).json())[0]["id"]
    engine = _engine(client)

    # Just under the 3-hour default — 2h 59m.
    engine._cycle_log.started_at = datetime.now(UTC) - timedelta(hours=2, minutes=59)

    await tick()

    cycle = next(c for c in await (await client.get("/api/logs")).json() if c["id"] == cycle_id)
    assert cycle["ended_at"] is None, "cycle should still be running near but under timeout"
    assert engine.cycle_state.value == "running"


@pytest.mark.asyncio
async def test_cycle_timeout_is_read_live_from_config(client, fake_ha, tick) -> None:
    """``cycle_timeout_hours`` is re-read from the DB on every tick — shortening
    it mid-cycle takes effect on the next tick. Without this the engine would
    cache the value at cycle start and a stuck cycle could not be cut short
    until it ran the original full duration."""
    _seed_cold_room(fake_ha)
    await _create_heating_room(client)
    await tick()
    cycle_id = (await (await client.get("/api/logs")).json())[0]["id"]
    engine = _engine(client)

    # Cycle has been running 30 minutes — still well under the 3-hour default.
    engine._cycle_log.started_at = datetime.now(UTC) - timedelta(minutes=30)
    await tick()
    assert engine.cycle_state.value == "running", "30 min < default 3h timeout"

    # Operator tightens cycle_timeout_hours to 0.25 (15 min).  The 30-min-old
    # cycle is now over the new limit and the next tick should terminate it.
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"cycle_timeout_hours": 0.25})
    assert resp.status == 200

    await tick()

    closed = next(c for c in await (await client.get("/api/logs")).json() if c["id"] == cycle_id)
    assert closed["ended_at"] is not None, "tightened timeout must terminate the cycle"
    assert closed["ended_reason"] == "timeout"
    assert engine.cycle_state.value == "idle"
