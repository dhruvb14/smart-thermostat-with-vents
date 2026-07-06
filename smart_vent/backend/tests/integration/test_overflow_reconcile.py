"""Overflow-conditioning vents vs. reconciliation and hold release (Issue #422).

The RUNNING-state reconcile pass closes any open vent belonging to a room not
in the active cycle (#82). Overflow rooms (#237) are deliberately open during
the minimum-runtime hold despite not being active — before #422 the reconciler
re-closed them as "drift", and ``_apply_overflow_during_hold`` trusted its
remembered set (``to_open = desired − remembered``) so the vent was never
re-opened: overflow was silently defeated for the rest of the hold.

These tests pin three behaviors:
  - reconcile leaves overflow rooms' vents alone during a hold;
  - the overflow pass repairs a vent that was physically closed out from
    under it (judged against real vent state, not bookkeeping);
  - releasing the hold (#423) closes the overflow vents — overflow
    conditioning is defined as running *during* the hold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"
A_SENSOR = "sensor.room_a"
A_VENT = "cover.room_a_vent"
C_SENSOR = "sensor.room_c"
C_VENT = "cover.room_c_vent"


def _engine(client):
    return client.app["scheduler"]._engines[THERMO]


async def _setup_hold_with_overflow(client, fake_ha, tick) -> None:
    """Create room A (scheduled, cooling to 72) and room C (idle, hot → a
    tier-1 overflow candidate), enter the min-runtime hold with C's vent open
    as the overflow destination."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 80.0, "temperature": 78.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state(A_SENSOR, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(A_VENT, "open", {})
    # Room C: idle (no schedule), hot past the default_temp (70) + deadband →
    # tier-1 overflow candidate for a cooling cycle.
    fake_ha.seed_state(C_SENSOR, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(C_VENT, "open", {})

    resp = await client.post("/api/rooms", json={"name": "RoomA", "thermostat_entity_id": THERMO})
    room_a = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_a}/sensors", json={"entity_id": A_SENSOR})
    await client.post(
        f"/api/rooms/{room_a}/vents",
        json={"entity_id": A_VENT, "control_method": "open_close"},
    )
    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).time().replace(second=0, microsecond=0)
    end = (now + timedelta(hours=1)).time().replace(second=0, microsecond=0)
    await client.post(
        f"/api/rooms/{room_a}/schedules",
        json={
            "days_of_week": list(range(7)),
            "start_time": start.isoformat(timespec="minutes"),
            "end_time": end.isoformat(timespec="minutes"),
            "target_temp": 72.0,
        },
    )
    resp = await client.post("/api/rooms", json={"name": "RoomC", "thermostat_entity_id": THERMO})
    room_c = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_c}/sensors", json={"entity_id": C_SENSOR})
    await client.post(
        f"/api/rooms/{room_c}/vents",
        json={"entity_id": C_VENT, "control_method": "open_close"},
    )

    await client.put(
        f"/api/thermostats/{THERMO}",
        json={
            "min_cycle_runtime_min": 15,
            "overflow_during_min_runtime": True,
            "reconciliation_interval_min": 1,
            "deadband": 1.0,
            # Room C has no per-room setpoint; the overflow candidate ranking
            # falls back to the thermostat default_temp (70 → C at 80 is a
            # tier-1 candidate for a cooling cycle).
            "default_temp": 70.0,
        },
    )

    await tick()  # cycle starts for A; idle C's vent is closed
    eng = _engine(client)
    assert eng.cycle_state.value == "running"
    assert fake_ha.get_state(C_VENT)["state"] == "closed"

    # A reaches target well before min runtime → hold engages, overflow
    # conditioning opens C.
    await fake_ha.set_entity_state(A_SENSOR, "72.0", {"unit_of_measurement": "°F"})
    await tick()
    assert eng._cycle_log.in_min_runtime_hold is True
    assert fake_ha.get_state(C_VENT)["state"] == "open", "overflow must open the candidate room"


@pytest.mark.asyncio
async def test_reconcile_does_not_close_overflow_vents_during_hold(client, fake_ha, tick) -> None:
    """A reconcile pass landing inside the hold must leave the overflow
    room's vent open — it is intentionally open, not drift (#422)."""
    await _setup_hold_with_overflow(client, fake_ha, tick)
    eng = _engine(client)

    # Force reconciliation to run on the next tick.
    eng._last_reconciled_at = None
    fake_ha.reset_calls()
    await tick()

    assert eng._cycle_log.in_min_runtime_hold is True, "still inside the hold"
    assert fake_ha.get_state(C_VENT)["state"] == "open", (
        "reconcile must not close an overflow room's vent as idle-room drift"
    )
    overflow_closes = [c for c in fake_ha.calls_for("close_cover") if c.data["entity_id"] == C_VENT]
    assert not overflow_closes, "no close command may target the overflow vent"


@pytest.mark.asyncio
async def test_overflow_reopens_vent_closed_out_from_under_it(client, fake_ha, tick) -> None:
    """The overflow pass must judge against PHYSICAL vent state: if something
    closed the overflow vent mid-hold, the next hold tick repairs it instead
    of trusting the remembered set and leaving it closed (#422)."""
    await _setup_hold_with_overflow(client, fake_ha, tick)
    eng = _engine(client)

    # Simulate an external actor (or a not-yet-fixed code path) closing the
    # overflow vent while the hold is in progress.
    await fake_ha.set_entity_state(C_VENT, "closed", {})
    await tick()

    assert eng._cycle_log.in_min_runtime_hold is True
    assert fake_ha.get_state(C_VENT)["state"] == "open", (
        "overflow must re-open a candidate whose vent is physically closed"
    )


@pytest.mark.asyncio
async def test_hold_release_closes_overflow_vents(client, fake_ha, tick) -> None:
    """When the hold is released because an active room drifted back past its
    deadband (#423), the overflow vents close — overflow conditioning only
    runs during the hold, and the resumed cycle should direct air at the room
    with live demand."""
    await _setup_hold_with_overflow(client, fake_ha, tick)
    eng = _engine(client)
    cycle_id = (await (await client.get("/api/logs")).json())[0]["id"]

    # Room A drifts past target + deadband (72 + 1.0) → hold releases.
    await fake_ha.set_entity_state(A_SENSOR, "73.5", {"unit_of_measurement": "°F"})
    await tick()

    assert eng._cycle_log.in_min_runtime_hold is False, "hold must release on drift past deadband"
    assert eng.cycle_state.value == "running", "cycle resumes — it does not terminate"
    assert fake_ha.get_state(C_VENT)["state"] == "closed", (
        "overflow vents must close when the hold releases"
    )
    detail = await (await client.get(f"/api/logs/{cycle_id}/detail")).json()
    close_events = [
        e
        for e in detail["vent_events"]
        if e["action"] == "closed_overflow_hold" and e["entity_id"] == C_VENT
    ]
    assert close_events, "the overflow close must be recorded in the cycle diagnostics"
    assert close_events[-1]["reason"] == "min-runtime hold released"
