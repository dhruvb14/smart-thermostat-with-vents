"""Minimum-runtime hold release on new or renewed demand (Issue #423).

Before #423 the ``in_min_runtime_hold`` flag was sticky: once set, the hold
exit terminated the cycle on runtime alone. A room that joined the cycle
mid-hold (presence/schedule), or a held room that drifted back out of its
comfort band, had its cycle terminated against live demand — and the
compressor off-time lockout (#208) then blocked the restart it needed.

Pinned here:
  - a room joining mid-hold releases the hold, and the cycle runs until the
    newcomer reaches target (no runtime-only termination);
  - a held room drifting past target + deadband releases the hold and the
    cycle re-conditions it back to target before terminating;
  - drift *within* the deadband keeps the hold (hysteresis) and does not
    block the runtime-satisfied termination;
  - after a drift release, re-reaching target before minimum runtime simply
    re-engages the hold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"
A_SENSOR = "sensor.room_a"
A_VENT = "cover.room_a_vent"
B_SENSOR = "sensor.room_b"
B_VENT = "cover.room_b_vent"


def _engine(client):
    return client.app["scheduler"]._engines[THERMO]


async def _make_room(client, name: str, sensor: str, vent: str) -> str:
    resp = await client.post("/api/rooms", json={"name": name, "thermostat_entity_id": THERMO})
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": sensor})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": vent, "control_method": "open_close"},
    )
    return room_id


async def _add_all_day_schedule(client, room_id: str, target_temp: float) -> None:
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


async def _enter_hold_single_room(client, fake_ha, tick):
    """Start a cooling cycle for room A and drive it into the hold."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 80.0, "temperature": 78.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state(A_SENSOR, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(A_VENT, "open", {})

    room_a = await _make_room(client, "RoomA", A_SENSOR, A_VENT)
    await _add_all_day_schedule(client, room_a, 72.0)
    await client.put(
        f"/api/thermostats/{THERMO}",
        json={
            "min_cycle_runtime_min": 15,
            "overflow_during_min_runtime": False,
            "deadband": 1.0,
        },
    )

    await tick()  # cycle starts
    eng = _engine(client)
    assert eng.cycle_state.value == "running"

    await fake_ha.set_entity_state(A_SENSOR, "72.0", {"unit_of_measurement": "°F"})
    await tick()  # target reached before min runtime → hold
    assert eng._cycle_log.in_min_runtime_hold is True
    return eng


@pytest.mark.asyncio
async def test_room_joining_mid_hold_releases_hold_and_extends_cycle(client, fake_ha, tick) -> None:
    """A cold room joining mid-hold must release the hold; the cycle then runs
    past minimum runtime until the newcomer reaches target."""
    fake_ha.seed_state(B_SENSOR, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(B_VENT, "closed", {})

    eng = await _enter_hold_single_room(client, fake_ha, tick)
    room_b = await _make_room(client, "RoomB", B_SENSOR, B_VENT)

    # RoomB gains demand mid-hold (schedule appears; presence would be the
    # same path — both add the room to the running cycle).
    await _add_all_day_schedule(client, room_b, 72.0)
    await tick()

    assert eng._cycle_log.in_min_runtime_hold is False, (
        "hold must release when unsatisfied demand joins mid-hold"
    )
    assert eng.cycle_state.value == "running"
    assert fake_ha.get_state(B_VENT)["state"] == "open", "the joining room's vent opens"

    # Minimum runtime elapses — the cycle must NOT terminate: RoomB is at
    # 80°F against a 72°F target. This is the #423 regression case (the old
    # hold exit terminated here on runtime alone).
    eng._cycle_log.started_at = datetime.now(UTC) - timedelta(minutes=16)
    await tick()
    assert eng.cycle_state.value == "running", (
        "cycle must keep running for the mid-hold joiner's live demand"
    )
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is None

    # RoomB reaches target → now the cycle completes.
    await fake_ha.set_entity_state(B_SENSOR, "72.0", {"unit_of_measurement": "°F"})
    await tick()
    assert eng.cycle_state.value == "idle"
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is not None


@pytest.mark.asyncio
async def test_drift_past_deadband_releases_hold_and_reconditions(client, fake_ha, tick) -> None:
    """A held room drifting past target + deadband regains live demand: the
    hold releases and the cycle keeps running until the room is back at
    target — even after minimum runtime is satisfied."""
    eng = await _enter_hold_single_room(client, fake_ha, tick)

    # Drift past 72.0 + deadband 1.0.
    await fake_ha.set_entity_state(A_SENSOR, "73.5", {"unit_of_measurement": "°F"})
    await tick()
    assert eng._cycle_log.in_min_runtime_hold is False, "hold must release on drift past deadband"
    assert eng.cycle_state.value == "running"
    assert fake_ha.get_state(A_VENT)["state"] == "open"

    # Runtime satisfied but the room is still off target → no termination.
    eng._cycle_log.started_at = datetime.now(UTC) - timedelta(minutes=16)
    await tick()
    assert eng.cycle_state.value == "running", "resumed cycle must run until back at target"

    # Back at target → cycle completes.
    await fake_ha.set_entity_state(A_SENSOR, "72.0", {"unit_of_measurement": "°F"})
    await tick()
    assert eng.cycle_state.value == "idle"
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is not None


@pytest.mark.asyncio
async def test_drift_within_deadband_keeps_hold_and_terminates_on_runtime(
    client, fake_ha, tick
) -> None:
    """Hysteresis: drift WITHIN the deadband neither releases the hold nor
    blocks the runtime-satisfied termination."""
    eng = await _enter_hold_single_room(client, fake_ha, tick)

    # 72.5 is inside 72.0 + deadband 1.0 — the hold must persist.
    await fake_ha.set_entity_state(A_SENSOR, "72.5", {"unit_of_measurement": "°F"})
    await tick()
    assert eng._cycle_log.in_min_runtime_hold is True, "within-deadband drift keeps the hold"
    assert eng.cycle_state.value == "running"

    # Runtime satisfied → terminates despite the small drift.
    eng._cycle_log.started_at = datetime.now(UTC) - timedelta(minutes=16)
    await tick()
    assert eng.cycle_state.value == "idle"
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is not None


@pytest.mark.asyncio
async def test_hold_reengages_after_drift_release_when_resatisfied_early(
    client, fake_ha, tick
) -> None:
    """After a drift release, a room that re-reaches target before minimum
    runtime re-engages the hold (entry is idempotent), and the cycle then
    terminates once runtime is satisfied."""
    eng = await _enter_hold_single_room(client, fake_ha, tick)

    await fake_ha.set_entity_state(A_SENSOR, "73.5", {"unit_of_measurement": "°F"})
    await tick()
    assert eng._cycle_log.in_min_runtime_hold is False

    # Re-satisfied while runtime is still unsatisfied → hold re-engages.
    await fake_ha.set_entity_state(A_SENSOR, "72.0", {"unit_of_measurement": "°F"})
    await tick()
    assert eng._cycle_log.in_min_runtime_hold is True, "hold must re-engage after re-satisfying"
    assert eng.cycle_state.value == "running"

    eng._cycle_log.started_at = datetime.now(UTC) - timedelta(minutes=16)
    await tick()
    assert eng.cycle_state.value == "idle"
