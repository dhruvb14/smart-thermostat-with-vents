"""Idle mode-hygiene reconciliation (Issue #637).

A post-cycle parked setpoint is direction-specific — ``_parked_setpoint``
sits it above ambient for cooling, below for heating, on the assumption that
the thermostat's hvac_mode stays what it was at termination. Nothing enforced
that assumption and nothing noticed when it broke: a thermostat that flipped
to the opposite mode (or a stray ``heat_cool``, or a live ``off``) while
idle, with the parked setpoint left in place, produced a live equipment call
in the wrong direction that Plenum logged at INFO as routine for as long as
it ran.

The fix recovers the last legitimate conditioning direction from the most
recent CLOSED row in ``cycle_logs`` (nothing durable tracks it in memory
while idle — ``_cycle_ha_mode`` is cleared by ``_terminate_cycle``/
``_abort_cycle``) and, on the idle arm of ``_reconcile_state``, corrects a
live hvac_mode that disagrees with it by re-parking against a FRESH ambient
read — not replaying the historical parked value, which has generally gone
stale by the time the mismatch is noticed.

These tests drive the full app through real cycle termination via
``tick()``, so the live hvac_mode and parked setpoint are exactly what
``_terminate_cycle`` would leave behind, then simulate the external flip the
same way the production incident happened (a reconnect-driven mode change
with the parked setpoint left untouched).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend import db
from backend.engine.cycle_engine import CycleEngine, CycleState

THERMO = "climate.test_thermostat"
SENSOR = "sensor.room_temp"
VENT = "cover.room_vent"


def _engine(client):
    return client.app["scheduler"]._engines[THERMO]


def _mode_calls(fake_ha) -> list:
    return [c for c in fake_ha.calls_for("set_temperature") if c.data["entity_id"] == THERMO]


async def _create_room_with_schedule(client, target_temp: float) -> str:
    resp = await client.post("/api/rooms", json={"name": "Room", "thermostat_entity_id": THERMO})
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": SENSOR})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": VENT, "control_method": "open_close"},
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
    return room_id


async def _force_reconcile_gate_open(client) -> None:
    """Mirrors test_reconcile_gate.py: a short interval plus a cleared
    last-reconciled timestamp makes the very next tick run a reconcile pass."""
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"reconciliation_interval_min": 1})
    assert resp.status == 200
    _engine(client)._last_reconciled_at = None


async def _terminate_cooling_cycle(client, fake_ha, tick) -> None:
    """Drive a cooling cycle to completion — parks the thermostat in 'cool'
    mode (per _terminate_cycle) and writes a cycle_logs row with mode='cooling'."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 74.0, "temperature": 72.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state(SENSOR, "74.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})
    await _create_room_with_schedule(client, target_temp=72.0)
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"overshoot_delta": 2.0})
    assert resp.status == 200

    await tick()  # cooling cycle starts
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["mode"] == "cooling"

    await fake_ha.set_entity_state(SENSOR, "72.0", {"unit_of_measurement": "°F"})
    await tick()  # target reached → terminate + park
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is not None, "precondition: cycle should have completed"


async def _terminate_heating_cycle(client, fake_ha, tick) -> None:
    fake_ha.seed_state(
        THERMO,
        "heat",
        {"current_temperature": 68.0, "temperature": 70.0, "hvac_action": "heating"},
    )
    fake_ha.seed_state(SENSOR, "68.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})
    await _create_room_with_schedule(client, target_temp=70.0)
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"overshoot_delta": 2.0})
    assert resp.status == 200

    await tick()  # heating cycle starts
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["mode"] == "heating"

    await fake_ha.set_entity_state(SENSOR, "70.0", {"unit_of_measurement": "°F"})
    await tick()  # target reached → terminate + park
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is not None, "precondition: cycle should have completed"


@pytest.mark.asyncio
async def test_stray_heat_after_cooling_cycle_is_corrected(client, fake_ha, tick) -> None:
    """The headline incident: a cooling cycle terminates and parks 'cool',
    the thermostat flips to 'heat' externally (a reconnect-driven change) with
    the parked setpoint left in place — the next reconcile pass must warn and
    command 'cool' back at a FRESHLY parked setpoint, not the stale one."""
    await _terminate_cooling_cycle(client, fake_ha, tick)

    # External flip: mode → heat, ambient has moved on, parked setpoint left
    # untouched (the exact incident shape).
    await fake_ha.set_entity_state(
        THERMO,
        "heat",
        {
            "current_temperature": 76.0,
            "temperature": fake_ha.get_state(THERMO)["attributes"]["temperature"],
        },
    )
    await _force_reconcile_gate_open(client)
    fake_ha.reset_calls()

    await tick()

    events = await (await client.get("/api/logs/events?level=warning")).json()
    assert any("hvac_mode" in e["message"] and "cool" in e["message"].lower() for e in events), (
        events
    )

    corrections = _mode_calls(fake_ha)
    assert corrections, f"expected a corrective set_temperature call; calls={fake_ha.calls}"
    last = corrections[-1]
    assert last.data.get("hvac_mode") == "cool"
    # Freshly parked against the CURRENT ambient (76 + overshoot 2 = 78), not
    # the stale historical parked value from before the flip (74).
    assert last.data["temperature"] == pytest.approx(78.0), last.data


@pytest.mark.asyncio
async def test_stray_cool_after_heating_cycle_is_corrected(client, fake_ha, tick) -> None:
    """The mirror case."""
    await _terminate_heating_cycle(client, fake_ha, tick)

    await fake_ha.set_entity_state(
        THERMO,
        "cool",
        {
            "current_temperature": 64.0,
            "temperature": fake_ha.get_state(THERMO)["attributes"]["temperature"],
        },
    )
    await _force_reconcile_gate_open(client)
    fake_ha.reset_calls()

    await tick()

    corrections = _mode_calls(fake_ha)
    assert corrections, f"expected a corrective set_temperature call; calls={fake_ha.calls}"
    last = corrections[-1]
    assert last.data.get("hvac_mode") == "heat"
    # Ambient 64 − overshoot 2 = 62, freshly computed.
    assert last.data["temperature"] == pytest.approx(62.0), last.data


@pytest.mark.asyncio
async def test_off_after_cooling_cycle_is_corrected_not_preserved(client, fake_ha, tick) -> None:
    """A live 'off' is external interference too — Plenum is the sole
    intended controller while enabled, so 'off' is not treated as a state to
    preserve, exactly like a stray heat/cool flip."""
    await _terminate_cooling_cycle(client, fake_ha, tick)

    await fake_ha.set_entity_state(
        THERMO, "off", {"current_temperature": 75.0, "temperature": None}
    )
    await _force_reconcile_gate_open(client)
    fake_ha.reset_calls()

    await tick()

    corrections = _mode_calls(fake_ha)
    assert corrections, "a live 'off' after a completed cooling cycle must be corrected"
    last = corrections[-1]
    assert last.data.get("hvac_mode") == "cool"
    assert last.data["temperature"] == pytest.approx(77.0), last.data  # 75 + overshoot 2


@pytest.mark.asyncio
async def test_heat_cool_regression_no_change_in_message_or_remedy(client, fake_ha, tick) -> None:
    """The pre-existing exact-match `heat_cool` guard in `_do_tick` fires
    before `_maybe_reconcile` is ever reached in the same tick, so a
    completed cycle in cycle_logs sitting alongside it must not change its
    message or remedy — still an unconditional revert to 'off', with no
    mode-hygiene correction riding along on that same tick.

    A climate-entity state_changed dispatch reactively ticks the engine
    immediately (the scheduler subscribes to every climate entity), so
    setting the live state IS the tick under test here — no explicit
    ``tick()`` call. Calls are reset right beforehand so only this one
    reactive tick is captured."""
    await _terminate_cooling_cycle(client, fake_ha, tick)
    fake_ha.reset_calls()

    await fake_ha.set_entity_state(
        THERMO, "heat_cool", {"current_temperature": 74.0, "temperature": 74.0}
    )

    off_calls = [c for c in fake_ha.calls_for("set_hvac_mode") if c.data["entity_id"] == THERMO]
    assert off_calls, f"heat_cool must still be reverted; calls={fake_ha.calls}"
    assert off_calls[-1].data["hvac_mode"] == "off"
    # And the mode-hygiene correction (a set_temperature call) must NOT ALSO
    # fire on the same tick — the early guard returns before it is reached.
    assert not _mode_calls(fake_ha), (
        f"heat_cool must be handled solely by the pre-existing guard; calls={fake_ha.calls}"
    )

    events = await (await client.get("/api/logs/events")).json()
    assert any("reverted from heat_cool" in e["message"] for e in events), events


@pytest.mark.asyncio
async def test_restart_survives_with_matching_mode_no_command(client, fake_ha, tick) -> None:
    """The scenario that ruled out an in-memory design: terminate a cooling
    cycle, then build a genuinely fresh CycleEngine sharing only the DB
    connection (no in-memory state carried over from the old instance —
    mirrors test_idle_vents_closed_on_restore.py's restart simulation), tick
    it with nothing externally changed. The recovered DB direction agrees
    with the live mode, so there must be no warning and no command."""
    await _terminate_cooling_cycle(client, fake_ha, tick)

    scheduler = client.app["scheduler"]
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"reconciliation_interval_min": 1})
    assert resp.status == 200
    fake_ha.reset_calls()

    new_engine = CycleEngine(
        thermostat_entity_id=THERMO,
        ha=fake_ha,
        vent_ctrl=scheduler._vent_ctrl,
        broadcast=None,
        event_logger=scheduler._event_logger,
        get_enabled=lambda: True,
    )
    # Confirm nothing carried over from the old instance.
    assert new_engine.cycle_state == CycleState.IDLE
    assert new_engine._cycle_ha_mode is None
    assert new_engine._last_reconciled_at is None

    # Mirror the scheduler's own _tick_engine sequence (scheduler.py), which
    # always loads room sensors immediately before ticking — a fresh engine
    # otherwise has no _sensor_map and treats the room's sensor as
    # unavailable, incorrectly falling back to thermostat ambient for the
    # mode vote (a pre-existing, unrelated behavior this test must not trip).
    zone_rooms = await db.get_rooms_for_thermostat(scheduler._db_conn, THERMO)
    await new_engine.load_room_sensors(scheduler._db_conn, [r.id for r in zone_rooms])

    await new_engine.tick(scheduler._db_conn)

    assert not _mode_calls(fake_ha), "a matching recovered mode must not be re-commanded"
    events = await (await client.get("/api/logs/events?level=warning")).json()
    assert not any("hvac_mode" in e["message"] and "disagrees" in e["message"] for e in events), (
        events
    )


@pytest.mark.asyncio
async def test_no_completed_cycle_draws_no_warning_or_command(client, fake_ha, tick) -> None:
    """A thermostat that has never completed a cycle has nothing to compare
    against — a stray hvac_mode must be left alone regardless."""
    fake_ha.seed_state(
        THERMO, "heat", {"current_temperature": 70.0, "temperature": 68.0, "hvac_action": "idle"}
    )
    # A room must exist for the scheduler to create an engine for THERMO, but
    # with no schedule/presence it stays idle with zero demand.
    await client.post("/api/rooms", json={"name": "Room", "thermostat_entity_id": THERMO})
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"reconciliation_interval_min": 1})
    assert resp.status == 200
    logs = await (await client.get("/api/logs")).json()
    assert logs == [], "precondition: no cycle history at all"

    await tick()

    assert not _mode_calls(fake_ha)
    events = await (await client.get("/api/logs/events?level=warning")).json()
    assert not any("hvac_mode" in e["message"] and "disagrees" in e["message"] for e in events)


@pytest.mark.asyncio
async def test_vacation_hold_off_survives_the_same_tick_reconcile(client, fake_ha, tick) -> None:
    """Vacation parity (Issue #637 follow-up — a real regression the first
    version of this fix shipped with, caught in review): `_apply_vacation_
    hold` owns this thermostat while vacation is active, and its own comment
    at the call site already warns "running both would issue competing
    setpoint commands on the same tick" — the idle-mode correction must not
    be the second thing issuing one.

    This drives the actual production call-site ordering `_do_tick` uses at
    its vacation-inclusive `_maybe_reconcile` call sites: `_apply_vacation_
    hold` first (commands `off` — ambient is comfortably in-band), then
    `_maybe_reconcile(conn, in_vacation=True, ...)` immediately after, on the
    same tick. The hold's `off` must survive.

    Replaces an earlier version of this test that only monkeypatched
    `eng._get_vacation_mode` — inert, because `_reconcile_state` never reads
    that getter; it only ever learns vacation status through the
    `in_vacation` parameter `_do_tick` threads through, which this test now
    passes for real. `_apply_vacation_hold`'s own HA calls update the fake's
    state directly without dispatching subscribers (unlike
    `fake_ha.set_entity_state`), so this reproduces the exact same-tick
    ordering without a contaminating reactive tick in between."""
    await _terminate_cooling_cycle(client, fake_ha, tick)  # recovered direction = 'cool'

    # Ambient has settled comfortably in-band; live mode is still 'cool',
    # left over from termination — the hold's job is to notice the trip is
    # quiet and turn the HVAC off.
    fake_ha.seed_state(THERMO, "cool", {"current_temperature": 70.0, "temperature": 72.0})
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"reconciliation_interval_min": 1})
    assert resp.status == 200

    eng = _engine(client)
    conn = client.app["scheduler"]._db_conn
    fake_ha.reset_calls()

    thermo_state = fake_ha.get_state(THERMO)
    await eng._apply_vacation_hold(conn, thermo_state)
    off_calls = [c for c in fake_ha.calls_for("set_hvac_mode") if c.data["entity_id"] == THERMO]
    assert off_calls and off_calls[-1].data["hvac_mode"] == "off", (
        f"precondition: the hold must turn the HVAC off in-band; calls={fake_ha.calls}"
    )
    assert fake_ha.get_state(THERMO)["state"] == "off"
    fake_ha.reset_calls()
    eng._last_reconciled_at = None

    # Same tick, immediately after — exactly what `_do_tick` calls at this
    # point on its vacation-inclusive arms.
    await eng._maybe_reconcile(conn, in_vacation=True)

    assert not _mode_calls(fake_ha), (
        f"the vacation hold's off must survive the same-tick reconcile pass; calls={fake_ha.calls}"
    )
    assert fake_ha.get_state(THERMO)["state"] == "off"


@pytest.mark.asyncio
async def test_in_vacation_threaded_end_to_end_at_no_compatible_rooms_branch(
    client, fake_ha, tick
) -> None:
    """End-to-end coverage that `_do_tick` itself supplies `in_vacation=True`
    at its 'no compatible rooms after filtering' call site. The sibling test
    above proves `_reconcile_state` honors the flag once handed it directly
    (calling `_apply_vacation_hold` then `_maybe_reconcile(in_vacation=True)`
    by hand) — this proves `_do_tick` actually SUPPLIES that value there,
    which the direct-call version cannot: it never goes through `_do_tick`.

    A vacation safety cycle starts in one direction (a room breaches the
    ceiling, `_add_safety_rooms` creates demand, the room's own vote and the
    inferred mode agree, so a normal cycle starts). The room's reading then
    swings hard to the opposite side of the envelope while the cycle is
    RUNNING: because the cycle's mode is LOCKED, `_filter_rooms_for_mode`
    drops the now-opposite-direction room, `new_active_map` empties, the
    cycle aborts (closing its cycle_logs row with mode='cooling' — the
    direction the idle-mode comparison will recover), and `_do_tick` falls
    into the vacation-hold branch immediately followed by `_maybe_reconcile`
    at this exact call site. The hold's own off-command must survive."""
    await client.post(
        "/api/thermostats",
        json={
            "thermostat_entity_id": THERMO,
            "total_vents_count": 1,
            "min_setpoint": 62.0,
            "max_setpoint": 78.0,
            "deadband": 2.0,
            "overshoot_delta": 2.0,
            "reconciliation_interval_min": 1,
        },
    )
    resp = await client.post("/api/rooms", json={"name": "Room", "thermostat_entity_id": THERMO})
    room_id = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": SENSOR})
    await client.post(
        f"/api/rooms/{room_id}/vents", json={"entity_id": VENT, "control_method": "open_close"}
    )

    resp = await client.post(
        "/api/settings/vacation-mode",
        json={"return_at": (datetime.now(UTC) + timedelta(days=7)).isoformat()},
    )
    assert resp.status == 200

    # Ambient in-band (still 75, safely above min(targets) − deadband = 74 —
    # see below) and the room sensor breaches the ceiling → a vacation safety
    # cooling cycle starts. Thermostat state seeded 'cool' (matching the
    # direction the cycle is about to command) so the hold's later
    # off-branch actually has something to change — the fake HA only updates
    # its tracked state via set_thermostat_hvac_mode, never via a bare
    # set_temperature(..., hvac_mode=X) call, so 'cool' persists unchanged
    # through the cycle start/abort below regardless of what those calls'
    # hvac_mode kwargs say.
    #
    # Ambient must clear 74 (target 76 − deadband 2) or `_infer_mode_from_
    # room_temps`'s ambient-sanity cross-check flips the vote to 'heating'
    # before a cycle ever starts — a DIFFERENT, earlier mechanism than the
    # one this test targets (a RUNNING cycle's room later flipping
    # direction); 75 keeps this test on the intended path.
    fake_ha.seed_state(
        THERMO, "cool", {"current_temperature": 75.0, "temperature": 75.0, "hvac_action": "idle"}
    )
    fake_ha.seed_state(SENSOR, "85.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})

    await tick()
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["mode"] == "cooling" and logs[0]["ended_at"] is None, (
        f"precondition: a vacation safety cooling cycle should be running; logs={logs}"
    )

    eng = _engine(client)
    eng._last_reconciled_at = None

    # The room's reading swings hard to the opposite side of the envelope —
    # the locked 'cooling' mode makes this room incompatible now. Sensor
    # changes don't dispatch a reactive tick (the scheduler only subscribes
    # to climate/binary_sensor entities), so the explicit tick() below is the
    # only evaluation of this new state.
    await fake_ha.set_entity_state(SENSOR, "50.0", {"unit_of_measurement": "°F"})
    fake_ha.reset_calls()

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is not None, "the cycle must abort — the room now needs heat"
    assert "no compatible rooms" in (logs[0]["ended_reason"] or ""), logs[0]

    off_calls = [c for c in fake_ha.calls_for("set_hvac_mode") if c.data["entity_id"] == THERMO]
    assert off_calls and off_calls[-1].data["hvac_mode"] == "off", (
        f"precondition: the vacation hold should turn the HVAC off in-band; calls={fake_ha.calls}"
    )

    # Exactly one set_temperature call is legitimate here and is not mine to
    # touch: `_abort_cycle` parks the just-aborted cycle on its own idle side
    # (cool@ambient+overshoot — active-cycle machinery, out of scope). A
    # SECOND call is exactly what a non-deferring idle-mode correction would
    # add: the hold's fresh 'off' disagrees with the just-recorded 'cooling'
    # history, so it would revert toward 'cool' again, layered under the
    # hold's own (set_hvac_mode-only) off command.
    assert len(_mode_calls(fake_ha)) == 1, (
        f"expected only the abort's own park — a second set_temperature call "
        f"means the idle-mode correction fired on top of the vacation hold; "
        f"calls={fake_ha.calls}"
    )
