"""The vacation hold's event-log visibility (Issue #627).

``_apply_vacation_hold`` is the strategy for every vacation tick with nothing
breaching, and the ONLY strategy when ``vacation_safety_cycles`` is off or the
thermostat holds a ``heat_cool`` range (#626). It commanded heat, cool and off
for days at a time while writing exactly one ``event_log`` row in 155 lines —
the compressor-lockout deferral — so from the Logs page a hold that was working
was indistinguishable from a hold that was doing nothing. That is what made
#626 hard to diagnose.

``test_cycle_engine_gaps_c.py::TestVacationHoldAnnouncements`` pins the
transition logic call-by-call. This file asserts the same consequence through
the real stack the operator actually sees: the scheduler's tick, the thermostat
config written over the API, and the rows that come back from
``GET /api/logs/events``. Rate-limiting is asserted the same way it is
implemented — once per transition — because the hold re-evaluates every 60 s and
a multi-day trip would otherwise bury the Live Feed (#211/#270).

Every temperature here is °F — the engine never converts (see CLAUDE.md).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"


async def _configure(
    client,
    *,
    min_setpoint: float = 62.0,
    max_setpoint: float = 80.0,
    vacation_hvac_mode: str = "single",
    vacation_safety_cycles: bool = False,
) -> None:
    """Register the thermostat with safety cycles OFF by default.

    That is the #624 opt-out, and one of the three paths that still run the
    bare hold — the others being range mode and any tick with nothing
    breaching. With cycles off the hold is unambiguously the only thing
    driving the thermostat, so every event below is the hold's own.
    """
    resp = await client.post(
        "/api/thermostats",
        json={
            "thermostat_entity_id": THERMO,
            "total_vents_count": 6,
            "min_setpoint": min_setpoint,
            "max_setpoint": max_setpoint,
            "deadband": 2.0,
            "overshoot_delta": 2.0,
            "vacation_hvac_mode": vacation_hvac_mode,
            "vacation_safety_cycles": vacation_safety_cycles,
        },
    )
    assert resp.status in (200, 201), await resp.text()


async def _enable_vacation(client) -> None:
    return_at = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    resp = await client.post("/api/settings/vacation-mode", json={"return_at": return_at})
    assert resp.status == 200


async def _hold_events(client) -> list[dict]:
    """Every Live Feed row the vacation hold wrote, oldest first."""
    events = await (await client.get("/api/logs/events?limit=200")).json()
    holds = [e for e in events if "Vacation hold" in e["message"]]
    holds.reverse()  # the endpoint returns newest-first
    return holds


async def _make_room(client, fake_ha) -> str:
    """One room with a vent and an in-envelope sensor.

    The scheduler only builds an engine for a thermostat that has at least one
    room, so a hold with nothing to hold still needs one. The sensor sits
    comfortably inside the envelope so ``_add_safety_rooms`` never manufactures
    demand and the hold stays the only thing driving the thermostat.
    """
    resp = await client.post("/api/rooms", json={"name": "Den", "thermostat_entity_id": THERMO})
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.den_temp"})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": "cover.den_vent", "control_method": "open_close"},
    )
    fake_ha.seed_state("sensor.den_temp", "70.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.den_vent", "open", {})
    return room_id


def _ambient(fake_ha, temp: float | None) -> None:
    """Move the thermostat's own probe, leaving everything the hold wrote to
    the thermostat (mode, setpoint, range) in place — so the idempotence check
    (#434/#296) sees the same thing a real thermostat would report back."""
    cur = fake_ha.get_state(THERMO) or {"state": "off", "attributes": {}}
    attrs = dict(cur.get("attributes") or {})
    attrs["current_temperature"] = temp
    attrs.setdefault("hvac_action", "idle")
    fake_ha.seed_state(THERMO, str(cur.get("state", "off")), attrs)


@pytest.mark.asyncio
async def test_a_range_mode_trip_is_visible_in_the_live_feed(client, fake_ha, tick) -> None:
    """The acceptance criterion for range mode.

    Safety cycles are structurally unavailable there — heat_cool hands the
    heat/cool decision to the equipment and the engine cannot lock a direction
    it does not own (#26/#29) — so the hold is the only mechanism, and it used
    to say nothing at all for the entire trip.
    """
    await _configure(client, vacation_hvac_mode="range")
    await _make_room(client, fake_ha)
    _ambient(fake_ha, 70.0)
    await _enable_vacation(client)

    for _ in range(6):  # stand-in for a multi-day trip
        await tick()

    holds = await _hold_events(client)
    assert len(holds) == 1, f"exactly one event for the trip, got {holds}"
    message = holds[0]["message"]
    assert "62.0°F" in message and "80.0°F" in message, message
    assert holds[0]["level"] == "info"
    # The hold really did take the thermostat — the event is not decoration.
    ranges = [c for c in fake_ha.calls_for("set_temperature") if c.data["entity_id"] == THERMO]
    assert ranges, f"the hold must command the range; calls={fake_ha.calls}"


@pytest.mark.asyncio
async def test_heat_cool_and_off_each_write_one_event_per_transition(client, fake_ha, tick) -> None:
    """A whole trip in one test: inside the band, a cold snap, a recovery and a
    heat wave. Four transitions, four events — and the repeated ticks inside
    each state add nothing, which is the half of the fix that keeps a week of
    vacation from burying the feed.
    """
    await _configure(client)
    await _make_room(client, fake_ha)
    _ambient(fake_ha, 70.0)
    await _enable_vacation(client)

    engine = client.app["scheduler"]._engines[THERMO]

    async def _settle(temp: float, ticks: int = 3) -> None:
        for _ in range(ticks):
            # Issue #636's plausibility guard rate-limits ambient change;
            # back-date the accepted-reading baseline before each step so
            # this narrative "whole trip" sequence — real jumps spread over
            # days, not one instantaneous tick — is not itself read as a
            # glitch. Also clear the real-time-windowed eval memo (round 2)
            # so each step is a fresh evaluation rather than reusing an
            # earlier step's cached answer for `_AMBIENT_EVAL_MIN_INTERVAL_SEC`
            # real seconds.
            if engine._last_valid_ambient_at is not None:
                engine._last_valid_ambient_at -= timedelta(minutes=20)
            engine._ambient_eval_at = None
            _ambient(fake_ha, temp)
            await tick()

    await _settle(70.0)  # inside the band
    await _settle(55.0)  # below min_setpoint
    await _settle(71.0)  # back inside
    await _settle(88.0)  # above max_setpoint

    holds = await _hold_events(client)
    assert len(holds) == 4, f"one event per transition, got {[h['message'] for h in holds]}"
    assert "inside the vacation band" in holds[0]["message"], holds[0]
    assert "below min_setpoint" in holds[1]["message"] and "55.0°F" in holds[1]["message"]
    assert "inside the vacation band" in holds[2]["message"], holds[2]
    assert "above max_setpoint" in holds[3]["message"] and "88.0°F" in holds[3]["message"]
    assert {h["level"] for h in holds} == {"info"}


@pytest.mark.asyncio
async def test_a_thermostat_with_no_ambient_reading_says_so_once(client, fake_ha, tick) -> None:
    """The bail-out that most resembles a dead system: the thermostat is
    reachable, so the #270 outage warning never fires, but it reports no
    current temperature — the hold cannot compare anything against the band
    and silently stops deciding."""
    await _configure(client)
    await _make_room(client, fake_ha)
    _ambient(fake_ha, None)
    await _enable_vacation(client)

    for _ in range(4):
        await tick()

    holds = await _hold_events(client)
    assert len(holds) == 1, f"one event per episode, got {holds}"
    assert holds[0]["level"] == "warning"
    assert "no ambient reading" in holds[0]["message"], holds[0]

    # A reading coming back is a transition: the feed shows the hold resume.
    # Issue #636 round 2: the guard's real-time-windowed memo would otherwise
    # keep reusing the first tick's cached "no reading" for
    # `_AMBIENT_EVAL_MIN_INTERVAL_SEC` real seconds — clear it so this tick
    # actually evaluates the returning 70.0°F reading.
    engine = client.app["scheduler"].get_engine(THERMO)
    engine._ambient_eval_at = None
    _ambient(fake_ha, 70.0)
    await tick()

    holds = await _hold_events(client)
    assert [h["level"] for h in holds] == ["warning", "info"], holds


@pytest.mark.asyncio
async def test_a_second_trip_announces_its_own_hold(client, fake_ha, tick) -> None:
    """The posture is scoped to one trip. Without the reset when vacation ends,
    a later trip opening in the state the previous one closed in would inherit
    its posture and stay silent for its whole duration."""
    await _configure(client)
    await _make_room(client, fake_ha)
    _ambient(fake_ha, 70.0)
    await _enable_vacation(client)
    await tick()
    assert len(await _hold_events(client)) == 1

    resp = await client.delete("/api/settings/vacation-mode")
    assert resp.status == 200
    await tick()  # home again — the episode is over

    await _enable_vacation(client)
    _ambient(fake_ha, 70.0)
    await tick()

    holds = await _hold_events(client)
    assert len(holds) == 2, f"the new trip must announce its own hold, got {holds}"
