"""Idle safety-setpoint backstop integration tests (Issue #367).

The per-room ``max_setpoint`` / ``min_setpoint`` hard cap only runs on the
active-room code path. When no room has demand (empty house — no schedule,
presence, or override), ``_do_tick`` returns on a ``not new_active_map``
branch before any cap is evaluated, so the thermostat is left ``off`` while
the space drifts past the configured envelope. This was the production bug:
after a vacation hold ended, the upstairs zone climbed to 81°F against a 77°F
ceiling with no cycle ever starting.

The thermostat-ambient backstop is the **last-resort fallback for a zone whose
rooms have no usable sensor reading** — when a room sensor is readable and
breaches, per-room safety protection (``_add_safety_rooms``, see
``test_safety_room_protection.py``) activates that room into a real cycle
*before* the no-active-rooms branch is reached, so the backstop never runs.
These tests therefore drive the breach through the **thermostat probe** with
the room sensor unavailable, which is the exact situation the backstop guards.

These tests drive the full engine against a fake Home Assistant and verify the
backstop:

  - engages a cooling command at ``max_setpoint`` when idle and ambient is
    above the ceiling;
  - engages a heating command at ``min_setpoint`` when idle and ambient is
    below the floor;
  - stays inert inside the envelope;
  - does not re-assert the setpoint when the thermostat is already holding the
    bound (no needless write traffic — Issue #296);
  - never preempts a normal per-room cycle when a room actually has demand;
  - is suppressed while the whole system is disabled (respects the off switch).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"
SENSOR = "sensor.test_room_temp"
VENT = "cover.test_room_vent"


async def _configure_thermostat(client, *, min_setpoint: float, max_setpoint: float) -> None:
    resp = await client.post(
        "/api/thermostats",
        json={
            "thermostat_entity_id": THERMO,
            "total_vents_count": 6,
            "min_setpoint": min_setpoint,
            "max_setpoint": max_setpoint,
            "overshoot_delta": 2.0,
            # Reconcile every tick so the idle path is fully exercised alongside
            # the backstop, matching how the real instance interleaves them.
            "reconciliation_interval_min": 1,
        },
    )
    assert resp.status in (200, 201)


async def _make_idle_room(client) -> str:
    """Create a room with a sensor + vent but NO schedule/presence.

    With no demand source the room never becomes active, so the engine takes
    the ``not new_active_map`` branch every tick — the exact gap the backstop
    guards. A room must exist for the scheduler to spin up a CycleEngine for
    this thermostat at all.
    """
    resp = await client.post("/api/rooms", json={"name": "Bedroom", "thermostat_entity_id": THERMO})
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": SENSOR})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": VENT, "control_method": "open_close"},
    )
    return room_id


async def _make_room_with_schedule(client, *, target_temp: float) -> str:
    room_id = await _make_idle_room(client)
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
    return room_id


async def _warnings(client) -> list[str]:
    events = await (await client.get("/api/logs/events?level=warning")).json()
    return [e["message"] for e in events]


@pytest.mark.asyncio
async def test_backstop_cools_to_max_when_idle_and_above_ceiling(client, fake_ha, tick) -> None:
    """Empty house, ambient above max_setpoint → command cool to max_setpoint.

    This is the exact production scenario: thermostat ``off`` (left behind by
    the post-vacation revert), no active rooms, ambient drifting past the cap.
    """
    await _configure_thermostat(client, min_setpoint=62.0, max_setpoint=77.0)
    await _make_idle_room(client)

    # Thermostat is OFF and the space has drifted to 81°F — 4°F over the cap.
    # The room sensor is unavailable, so per-room safety can't act and the
    # thermostat-ambient backstop is the mechanism under test.
    fake_ha.seed_state(
        THERMO,
        "off",
        {"current_temperature": 81.0, "temperature": None, "hvac_action": "idle"},
    )
    fake_ha.seed_state(SENSOR, "unavailable", {})
    fake_ha.seed_state(VENT, "open", {})

    await tick()

    # No cycle was started — the backstop drives the thermostat directly.
    assert (await (await client.get("/api/logs")).json()) == []
    assert client.app["scheduler"]._engines[THERMO].cycle_state.value == "idle"

    # The thermostat was commanded to cool to exactly the ceiling.
    sp_calls = fake_ha.calls_for("set_temperature")
    assert sp_calls, f"backstop did not command a setpoint; got {fake_ha.calls}"
    last = sp_calls[-1]
    assert last.data["temperature"] == pytest.approx(77.0)
    assert last.data["hvac_mode"] == "cool"

    warnings = await _warnings(client)
    assert any("Safety backstop engaged" in m and "77.0" in m for m in warnings), warnings


@pytest.mark.asyncio
async def test_backstop_heats_to_min_when_idle_and_below_floor(client, fake_ha, tick) -> None:
    """Empty house, ambient below min_setpoint → command heat to min_setpoint."""
    await _configure_thermostat(client, min_setpoint=62.0, max_setpoint=77.0)
    await _make_idle_room(client)

    # Thermostat OFF, space has dropped to 55°F — 7°F under the floor. Room
    # sensor unavailable, so the thermostat-ambient backstop is what acts.
    fake_ha.seed_state(
        THERMO,
        "off",
        {"current_temperature": 55.0, "temperature": None, "hvac_action": "idle"},
    )
    fake_ha.seed_state(SENSOR, "unavailable", {})
    fake_ha.seed_state(VENT, "open", {})

    await tick()

    assert client.app["scheduler"]._engines[THERMO].cycle_state.value == "idle"
    sp_calls = fake_ha.calls_for("set_temperature")
    assert sp_calls, f"backstop did not command a setpoint; got {fake_ha.calls}"
    last = sp_calls[-1]
    assert last.data["temperature"] == pytest.approx(62.0)
    assert last.data["hvac_mode"] == "heat"

    warnings = await _warnings(client)
    assert any("Safety backstop engaged" in m and "62.0" in m for m in warnings), warnings


@pytest.mark.asyncio
async def test_backstop_inert_within_envelope(client, fake_ha, tick) -> None:
    """Idle and comfortably within bounds → the backstop does nothing."""
    await _configure_thermostat(client, min_setpoint=62.0, max_setpoint=77.0)
    await _make_idle_room(client)

    fake_ha.seed_state(
        THERMO,
        "off",
        {"current_temperature": 70.0, "temperature": None, "hvac_action": "idle"},
    )
    fake_ha.seed_state(SENSOR, "70.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})

    await tick()

    assert fake_ha.calls_for("set_temperature") == [], "no setpoint should be commanded in-band"
    warnings = await _warnings(client)
    assert not any("Safety backstop engaged" in m for m in warnings), warnings


@pytest.mark.asyncio
async def test_backstop_does_not_reassert_when_already_holding(client, fake_ha, tick) -> None:
    """Already cooling at the ceiling → no redundant re-command (Issue #296).

    Once the backstop has driven the thermostat to ``cool`` at ``max_setpoint``,
    re-sending the identical setpoint every 60 s tick is needless write traffic
    that can hit cloud-thermostat rate limits.
    """
    await _configure_thermostat(client, min_setpoint=62.0, max_setpoint=77.0)
    await _make_idle_room(client)

    # Thermostat is ALREADY cooling at the ceiling; the space is still working
    # its way back down (81°F > 77°F) but no fresh command is needed. Room
    # sensor unavailable so the backstop (not per-room safety) is exercised.
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 81.0, "temperature": 77.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state(SENSOR, "unavailable", {})
    fake_ha.seed_state(VENT, "open", {})

    await tick()

    assert fake_ha.calls_for("set_temperature") == [], (
        f"backstop re-asserted an already-held setpoint; got {fake_ha.calls}"
    )


@pytest.mark.asyncio
async def test_backstop_does_not_preempt_normal_cycle(client, fake_ha, tick) -> None:
    """A room with real demand → a normal per-room cycle runs, not the backstop.

    With an active room the engine never reaches the no-active-rooms branch, so
    the cycle targets the room (cool to 70 − overshoot), well below the 77°F
    safety ceiling, and no backstop warning is emitted.
    """
    await _configure_thermostat(client, min_setpoint=62.0, max_setpoint=77.0)
    await _make_room_with_schedule(client, target_temp=70.0)

    # Ambient is above the ceiling, but the scheduled room is what should drive
    # the cycle — proving the backstop defers to genuine demand.
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 81.0, "temperature": 81.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state(SENSOR, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1, f"a normal cooling cycle should start; got {logs}"
    assert logs[0]["mode"] == "cooling"

    # The commanded setpoint tracks the room target (68), not the safety cap (77).
    sp_calls = fake_ha.calls_for("set_temperature")
    assert sp_calls, "a normal cycle should command a setpoint"
    assert sp_calls[-1].data["temperature"] == pytest.approx(68.0, abs=0.5)

    warnings = await _warnings(client)
    assert not any("Safety backstop engaged" in m for m in warnings), warnings


@pytest.mark.asyncio
async def test_backstop_suppressed_when_system_disabled(client, fake_ha, tick) -> None:
    """System disabled → the backstop is silent (respects the off switch)."""
    await _configure_thermostat(client, min_setpoint=62.0, max_setpoint=77.0)
    await _make_idle_room(client)
    assert (await client.post("/api/system/enabled", json={"enabled": False})).status == 200

    fake_ha.seed_state(
        THERMO,
        "off",
        {"current_temperature": 81.0, "temperature": None, "hvac_action": "idle"},
    )
    fake_ha.seed_state(SENSOR, "81.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})

    await tick()

    assert fake_ha.calls_for("set_temperature") == [], (
        "a disabled system must not command the thermostat"
    )
    warnings = await _warnings(client)
    assert not any("Safety backstop engaged" in m for m in warnings), warnings
