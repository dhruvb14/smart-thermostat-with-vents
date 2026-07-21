"""A served room that drifts past its deadband mid-cycle gets its vent reopened.

Regression: ``_monitor_rooms`` only ever acted on rooms whose vent was still
open (``vent_closed_at is None``). A room that reached target early had its vent
closed and was then ignored for the rest of the cycle. While a *slower*
co-active room kept the cycle running, the served room could heat-/cool-soak far
past its target with its vent shut and nothing reopened it until the cycle
timeout (observed in production: an office at 73.9 °F against a 70.6 °F cooling
target, vent closed, for 30+ minutes).

#423 already re-engaged a drifted served room — but only inside the
minimum-runtime hold, which is reached only once *every* room is satisfied. This
generalizes it to normal monitoring: past a full deadband the room has live
demand again, so its vent reopens and the cycle keeps serving it.

Pinned here:
  - a served room drifting MORE than a deadband past target reopens its vent and
    the cycle keeps running until it is back at target (cooling and heating);
  - drift WITHIN the deadband keeps the vent closed — hysteresis, so the #86
    "served rooms don't block termination" guarantee is preserved and the vent
    cannot thrash at the target boundary;
  - the boundary (exactly target ± deadband) does not reopen; a touch past does;
  - the reopen is recorded as a ``reopened_drift`` vent event for the operator.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"
A_SENSOR = "sensor.room_a"
A_VENT = "cover.room_a_vent"
B_SENSOR = "sensor.room_b"
B_VENT = "cover.room_b_vent"

_ATTRS = {"unit_of_measurement": "°F"}


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


async def _start_two_room_cycle(
    client,
    fake_ha,
    tick,
    *,
    mode: str,
    thermo_ambient: float,
    a_temp: float,
    b_temp: float,
    target: float,
):
    """Start a cycle with two rooms that both need conditioning.

    Room B never reaches target, so the cycle keeps running the whole test —
    exactly the state that used to strand a served Room A out of band. Minimum
    runtime is disabled so the min-runtime hold (whose own #423 path already
    handles drift) never engages and the normal-monitoring path is exercised.
    """
    hvac_action = "cooling" if mode == "cool" else "heating"
    fake_ha.seed_state(
        THERMO,
        mode,
        {"current_temperature": thermo_ambient, "temperature": target, "hvac_action": hvac_action},
    )
    fake_ha.seed_state(A_SENSOR, str(a_temp), _ATTRS)
    fake_ha.seed_state(A_VENT, "open", {})
    fake_ha.seed_state(B_SENSOR, str(b_temp), _ATTRS)
    fake_ha.seed_state(B_VENT, "open", {})

    room_a = await _make_room(client, "RoomA", A_SENSOR, A_VENT)
    room_b = await _make_room(client, "RoomB", B_SENSOR, B_VENT)
    await _add_all_day_schedule(client, room_a, target)
    await _add_all_day_schedule(client, room_b, target)
    await client.put(
        f"/api/thermostats/{THERMO}",
        json={"min_cycle_runtime_min": 0, "min_cycle_offtime_min": 0, "deadband": 1.0},
    )

    await tick()  # cycle starts
    eng = _engine(client)
    assert eng.cycle_state.value == "running"
    return eng, room_a, room_b


@pytest.mark.asyncio
async def test_served_room_reopens_on_drift_past_deadband_cooling(client, fake_ha, tick) -> None:
    eng, _room_a, _room_b = await _start_two_room_cycle(
        client,
        fake_ha,
        tick,
        mode="cool",
        thermo_ambient=80.0,
        a_temp=74.0,
        b_temp=80.0,
        target=72.0,
    )

    # RoomA reaches its 72°F target → vent closes. RoomB (80°F) keeps running.
    await fake_ha.set_entity_state(A_SENSOR, "72.0", _ATTRS)
    await tick()
    assert fake_ha.get_state(A_VENT)["state"] == "closed"
    assert fake_ha.get_state(B_VENT)["state"] == "open"
    assert eng.cycle_state.value == "running"

    # RoomA heat-soaks 1.5°F past its 72°F target (deadband 1.0) with its vent
    # shut — genuine renewed demand. The served-room reopen must fire.
    await fake_ha.set_entity_state(A_SENSOR, "73.5", _ATTRS)
    await tick()
    assert fake_ha.get_state(A_VENT)["state"] == "open", (
        "a served room past its deadband must have its vent reopened"
    )
    assert eng.cycle_state.value == "running"

    # And it is visible to the operator as a distinct vent event.
    cycle_id = eng._cycle_log.id
    detail = await (await client.get(f"/api/logs/{cycle_id}/detail")).json()
    a_actions = {e["action"] for e in detail["vent_events"] if e["entity_id"] == A_VENT}
    assert "reopened_drift" in a_actions

    # Cooled back to target → the normal close path re-closes it.
    await fake_ha.set_entity_state(A_SENSOR, "72.0", _ATTRS)
    await tick()
    assert fake_ha.get_state(A_VENT)["state"] == "closed"
    assert eng.cycle_state.value == "running"


@pytest.mark.asyncio
async def test_served_room_stays_closed_within_deadband(client, fake_ha, tick) -> None:
    """Hysteresis / #86: within-band drift must NOT reopen the vent, or the
    vent would thrash every time the served room re-crossed its target."""
    eng, _room_a, _room_b = await _start_two_room_cycle(
        client,
        fake_ha,
        tick,
        mode="cool",
        thermo_ambient=80.0,
        a_temp=74.0,
        b_temp=80.0,
        target=72.0,
    )

    await fake_ha.set_entity_state(A_SENSOR, "72.0", _ATTRS)
    await tick()
    assert fake_ha.get_state(A_VENT)["state"] == "closed"

    # 73.0 is exactly target + deadband — NOT past it (strict >) → stays closed.
    await fake_ha.set_entity_state(A_SENSOR, "73.0", _ATTRS)
    await tick()
    assert fake_ha.get_state(A_VENT)["state"] == "closed", (
        "boundary drift (exactly target + deadband) must not reopen"
    )
    assert eng.cycle_state.value == "running"

    # A touch past the band → reopens.
    await fake_ha.set_entity_state(A_SENSOR, "73.2", _ATTRS)
    await tick()
    assert fake_ha.get_state(A_VENT)["state"] == "open"


@pytest.mark.asyncio
async def test_served_room_reopens_on_drift_past_deadband_heating(client, fake_ha, tick) -> None:
    """Mirror image for heating: a served room cooling past target - deadband
    reopens."""
    eng, _room_a, _room_b = await _start_two_room_cycle(
        client,
        fake_ha,
        tick,
        mode="heat",
        thermo_ambient=60.0,
        a_temp=68.0,
        b_temp=60.0,
        target=72.0,
    )

    # RoomA warms to its 72°F target → vent closes. RoomB (60°F) keeps running.
    await fake_ha.set_entity_state(A_SENSOR, "72.0", _ATTRS)
    await tick()
    assert fake_ha.get_state(A_VENT)["state"] == "closed"
    assert eng.cycle_state.value == "running"

    # RoomA cools 1.5°F below its 72°F target (deadband 1.0) → vent reopens.
    await fake_ha.set_entity_state(A_SENSOR, "70.5", _ATTRS)
    await tick()
    assert fake_ha.get_state(A_VENT)["state"] == "open", (
        "a served room past its deadband must reopen in heating too"
    )
    assert eng.cycle_state.value == "running"
