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


# ---------------------------------------------------------------------------
# Issue #637 follow-up (a real regression the idle-mode-hygiene fix shipped
# with, caught in review): `_enforce_safety_setpoint` and `_maybe_reconcile`
# run on the SAME tick here (`_configure_thermostat` sets
# `reconciliation_interval_min: 1`, so the reconcile gate is open from the
# very first tick). Without deference, the idle-mode-hygiene comparison sees
# the backstop's freshly-commanded mode disagreeing with whatever direction
# the last COMPLETED cycle ran, decides that's "external interference", and
# reverts it — undoing the exact #367/#368 protection this backstop exists
# for. These seed real cycle_logs history (the earlier tests above seed none,
# which is why they could not have caught this: `get_last_completed_cycle_
# for_thermostat` always returns None there and the new code path never
# fires). The breach is left in place across a second tick to prove the
# deference holds for as long as the breach persists, not just once —
# `_enforce_safety_setpoint` returns True on every tick the bound stays
# breached, whether or not a fresh command is needed that tick.
# ---------------------------------------------------------------------------


async def _seed_completed_cycle(client, *, mode: str) -> None:
    from backend import db
    from backend.models import CycleLog

    conn = client.app["scheduler"]._db_conn
    cycle = CycleLog.create(thermostat_entity_id=THERMO, mode=mode, rooms_json="{}")
    await db.insert_cycle_log(conn, cycle)
    await db.close_cycle_log(conn, cycle.id, datetime.now(UTC), ended_reason="completed")


@pytest.mark.asyncio
async def test_backstop_cool_command_survives_reconcile_after_heating_history(
    client, fake_ha, tick
) -> None:
    """Last completed cycle was HEATING; ambient breaches the ceiling so the
    backstop correctly commands cool@max_setpoint. The idle-mode-hygiene
    comparison must defer to that command — not see 'cool' disagreeing with
    the recovered 'heating' direction and revert it to heat, arming a heat
    call on a house already over its own cooling ceiling."""
    await _configure_thermostat(client, min_setpoint=62.0, max_setpoint=77.0)
    await _make_idle_room(client)
    await _seed_completed_cycle(client, mode="heating")

    fake_ha.seed_state(
        THERMO,
        "off",
        {"current_temperature": 81.0, "temperature": None, "hvac_action": "idle"},
    )
    fake_ha.seed_state(SENSOR, "unavailable", {})
    fake_ha.seed_state(VENT, "open", {})

    await tick()

    sp_calls = fake_ha.calls_for("set_temperature")
    assert len(sp_calls) == 1, (
        f"expected exactly the backstop's own command, no revert on the same "
        f"tick; calls={fake_ha.calls}"
    )
    assert sp_calls[0].data["temperature"] == pytest.approx(77.0)
    assert sp_calls[0].data["hvac_mode"] == "cool"
    warnings = await _warnings(client)
    assert not any("disagrees" in m for m in warnings), warnings

    # The breach persists (ambient unchanged) — a second tick must not
    # re-fight it either, proving deference holds beyond the first tick.
    fake_ha.reset_calls()
    await tick()
    reverts = [c for c in fake_ha.calls_for("set_temperature") if c.data.get("hvac_mode") == "heat"]
    assert not reverts, f"the backstop's cool command must not be reverted; calls={fake_ha.calls}"


@pytest.mark.asyncio
async def test_backstop_heat_command_survives_reconcile_after_cooling_history(
    client, fake_ha, tick
) -> None:
    """Mirror case: last completed cycle was COOLING; ambient breaches the
    floor so the backstop correctly commands heat@min_setpoint. Must not be
    reverted to cool by the idle-mode-hygiene comparison."""
    await _configure_thermostat(client, min_setpoint=62.0, max_setpoint=77.0)
    await _make_idle_room(client)
    await _seed_completed_cycle(client, mode="cooling")

    fake_ha.seed_state(
        THERMO,
        "off",
        {"current_temperature": 55.0, "temperature": None, "hvac_action": "idle"},
    )
    fake_ha.seed_state(SENSOR, "unavailable", {})
    fake_ha.seed_state(VENT, "open", {})

    await tick()

    sp_calls = fake_ha.calls_for("set_temperature")
    assert len(sp_calls) == 1, (
        f"expected exactly the backstop's own command, no revert on the same "
        f"tick; calls={fake_ha.calls}"
    )
    assert sp_calls[0].data["temperature"] == pytest.approx(62.0)
    assert sp_calls[0].data["hvac_mode"] == "heat"
    warnings = await _warnings(client)
    assert not any("disagrees" in m for m in warnings), warnings

    fake_ha.reset_calls()
    await tick()
    reverts = [c for c in fake_ha.calls_for("set_temperature") if c.data.get("hvac_mode") == "cool"]
    assert not reverts, f"the backstop's heat command must not be reverted; calls={fake_ha.calls}"


@pytest.mark.asyncio
async def test_backstop_fired_threaded_end_to_end_at_no_compatible_rooms_branch(
    client, fake_ha, tick
) -> None:
    """`backstop_fired` must reach `_do_tick`'s OTHER `_enforce_safety_setpoint`
    call site too — the 'no compatible rooms after filtering' branch — not
    just the no-active-rooms branch the two tests above exercise.

    A normal (non-vacation) schedule-driven cooling cycle is RUNNING. The
    room's reading then swings hard to the opposite side of its target: the
    room now needs heat, which is the opposite of the cycle's LOCKED
    'cooling' mode, so `_filter_rooms_for_mode` drops it, `new_active_map`
    empties, and the cycle aborts — closing its cycle_logs row with
    mode='cooling' (the direction the idle-mode comparison will recover). In
    the SAME tick the thermostat's own ambient has also fallen below the
    floor, so `_enforce_safety_setpoint` fires a 'heat' command.

    The fake HA only updates its tracked ``state`` (hvac_mode) via
    `set_thermostat_hvac_mode` — a bare `set_thermostat_temperature(...,
    hvac_mode=X)` call (what both the cycle-abort park and the backstop use)
    changes only the setpoint attribute, so the live ``state`` this test
    seeds before tick 2 (deliberately left disagreeing with both the
    recovered 'cooling' history AND the backstop's own fresh 'heat') persists
    unchanged through the whole tick. That is exactly the situation
    `backstop_fired` exists to guard: regardless of why the live mode reads
    as a mismatch against history, the idle-mode comparison must defer
    entirely when the backstop just acted, not layer a second, competing
    command underneath it — Finding 1 recurring on this second branch."""
    await _configure_thermostat(client, min_setpoint=62.0, max_setpoint=77.0)
    await _make_room_with_schedule(client, target_temp=70.0)

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 81.0, "temperature": None, "hvac_action": "idle"}
    )
    fake_ha.seed_state(SENSOR, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})

    await tick()
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["mode"] == "cooling" and logs[0]["ended_at"] is None, (
        f"precondition: a cooling cycle should be running; logs={logs}"
    )

    eng = client.app["scheduler"]._engines[THERMO]
    eng._last_reconciled_at = None

    # The room's reading swings hard to the opposite side of its target.
    # set_entity_state is safe here (no reactive tick — the scheduler only
    # subscribes to climate/binary_sensor entities, not plain sensors). The
    # thermostat's own ambient also falls below the floor via seed_state
    # (NOT set_entity_state, so this settling does not itself dispatch a
    # reactive tick) — its "state" is left at "off" deliberately (see
    # docstring): the single explicit tick() below must be the only real
    # evaluation of this new state.
    await fake_ha.set_entity_state(SENSOR, "50.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 50.0, "temperature": 68.0, "hvac_action": "idle"}
    )
    fake_ha.reset_calls()

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is not None, "the cycle must abort — the room now needs heat"
    assert "no compatible rooms" in (logs[0]["ended_reason"] or ""), logs[0]

    sp_calls = fake_ha.calls_for("set_temperature")
    assert sp_calls, f"the backstop should command heat@min_setpoint; calls={fake_ha.calls}"
    # Exactly two commands are legitimate on this tick and neither is mine to
    # touch: `_abort_cycle` parks the just-aborted cycle on its own idle side
    # first (cool@ambient+overshoot — active-cycle machinery, out of scope),
    # THEN the backstop commands heat@min_setpoint. The backstop's command
    # must be the LAST word — nothing may follow it, which is exactly what a
    # non-deferring idle-mode correction would add (a third call, reverting
    # back toward 'cool' to match the recovered 'cooling' history the abort
    # just recorded, since the live 'off' this test seeded — per the fake-HA
    # quirk described in the docstring — disagrees with it regardless of what
    # the backstop itself just commanded).
    assert len(sp_calls) == 2, (
        f"expected exactly [abort's own park, the backstop's command] — a "
        f"third call means the idle-mode correction fired on top of the "
        f"backstop; calls={fake_ha.calls}"
    )
    last = sp_calls[-1]
    assert last.data["temperature"] == pytest.approx(62.0)
    assert last.data["hvac_mode"] == "heat"
