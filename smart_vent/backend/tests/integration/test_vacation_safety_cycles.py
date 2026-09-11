"""Per-room safety cycles during vacation mode (Issue #619).

Before this, vacation mode returned from ``_do_tick`` above BOTH
``get_active_rooms`` and ``_add_safety_rooms``, so a room breaching its
envelope while the house was empty produced no cycle, no cycle-history row and
(outside the compressor-lockout deferral) no event-log line. That is the
#367/#368 production incident one state over: there the zone baked at 81°F
against a 77°F ceiling *after* a vacation hold expired; the same room breaching
the same bound one minute earlier was invisible to the engine.

Vacation is now a demand FILTER. Schedules, presence and temporary holds stay
paused — a safety breach is the only thing that may create demand — and when
nothing is breaching, ``_apply_vacation_hold`` still owns the thermostat.

Two restrictions are asserted here rather than left to UI copy:

  * ``vacation_hvac_mode == "range"`` never runs safety cycles, because
    heat_cool hands the heat/cool decision to the equipment and the engine
    cannot lock a direction it does not own (#26/#29).
  * ``vacation_safety_cycles=False`` is a full opt-out back to the old hold.

The vent policy is deliberately NOT the #237 tier system: with nobody home the
zone runs wide open and each non-breaching room keeps absorbing air until it
nears its OWN opposite safety bound.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"


async def _configure(
    client,
    *,
    min_setpoint: float = 62.0,
    max_setpoint: float = 78.0,
    deadband: float = 2.0,
    vacation_hvac_mode: str = "single",
    vacation_safety_cycles: bool = True,
    total_vents_count: int = 6,
) -> None:
    await client.post(
        "/api/thermostats",
        json={
            "thermostat_entity_id": THERMO,
            "total_vents_count": total_vents_count,
            "min_setpoint": min_setpoint,
            "max_setpoint": max_setpoint,
            "deadband": deadband,
            "overshoot_delta": 2.0,
            "vacation_hvac_mode": vacation_hvac_mode,
            "vacation_safety_cycles": vacation_safety_cycles,
        },
    )


async def _make_room(
    client,
    name: str,
    sensor: str | None,
    vent: str,
    *,
    schedule_target: float | None = None,
) -> str:
    resp = await client.post("/api/rooms", json={"name": name, "thermostat_entity_id": THERMO})
    room_id: str = (await resp.json())["id"]
    if sensor is not None:
        await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": sensor})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": vent, "control_method": "open_close"},
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


async def _enable_vacation(client) -> None:
    return_at = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    resp = await client.post("/api/settings/vacation-mode", json={"return_at": return_at})
    assert resp.status == 200


def _vent_state(fake_ha, entity_id: str) -> str:
    state = fake_ha.get_state(entity_id)
    assert state is not None, f"{entity_id} missing from fake HA"
    return str(state["state"])


# ---------------------------------------------------------------------------
# The core gap: a breach during vacation must produce a real cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breaching_room_starts_a_cycle_during_vacation(client, fake_ha, tick) -> None:
    """The headline fix: gym at 85°F against a 78°F ceiling, house empty.

    Asserts the CONSEQUENCE (a cycle exists, cooling is commanded, the cycle
    is in history and the breach is in the event log) rather than a reason
    string — the pre-#619 complaint was precisely that none of these existed.
    """
    await _configure(client)
    gym = await _make_room(client, "Gym", "sensor.gym_temp", "cover.gym_vent")

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 78.0, "temperature": None, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.gym_temp", "85.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.gym_vent", "open", {})
    await _enable_vacation(client)

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1, f"a vacation safety cycle should exist; got {logs}"
    assert logs[0]["mode"] == "cooling"
    gym_meta = logs[0]["rooms"][gym]
    assert gym_meta["source"] == "safety"
    # Target is one deadband inside the breached ceiling: 78 − 2 = 76°F.
    assert gym_meta["target"] == pytest.approx(76.0)

    cools = [
        c
        for c in fake_ha.calls_for("set_temperature")
        if c.data["entity_id"] == THERMO and c.data.get("hvac_mode") == "cool"
    ]
    assert cools, f"the safety cycle must command cooling; calls={fake_ha.calls}"

    events = await (await client.get("/api/logs/events?level=warning")).json()
    assert any("Safety protection engaged for room 'Gym'" in e["message"] for e in events), events


@pytest.mark.asyncio
async def test_heating_breach_during_vacation_starts_a_heating_cycle(client, fake_ha, tick) -> None:
    """The other side of the envelope — a room below min_setpoint."""
    await _configure(client)
    den = await _make_room(client, "Den", "sensor.den_temp", "cover.den_vent")

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 66.0, "temperature": None, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.den_temp", "55.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.den_vent", "open", {})
    await _enable_vacation(client)

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1
    assert logs[0]["mode"] == "heating"
    # Target is one deadband inside the floor: 62 + 2 = 64°F.
    assert logs[0]["rooms"][den]["target"] == pytest.approx(64.0)


@pytest.mark.asyncio
async def test_room_inside_the_envelope_gets_no_cycle(client, fake_ha, tick) -> None:
    """Boundary, non-breaching side: exactly AT max_setpoint is not a breach.

    `_add_safety_rooms` tests `effective > max_setpoint` strictly, so 78.0
    against a ceiling of 78.0 must stay quiet and leave the hold in charge —
    the exact prod state that started this investigation.
    """
    await _configure(client)
    await _make_room(client, "Gym", "sensor.gym_temp", "cover.gym_vent")

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 78.0, "temperature": None, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.gym_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.gym_vent", "open", {})
    await _enable_vacation(client)

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert logs == [], f"78.0 is not > 78.0 — no cycle should start; got {logs}"


# ---------------------------------------------------------------------------
# Vacation stays a demand FILTER: only safety may create demand
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedules_stay_paused_during_vacation(client, fake_ha, tick) -> None:
    """A room with a live schedule block and no breach gets no cycle.

    This is the promise the vacation modal makes; #619 must not quietly turn
    vacation into normal operation with wider bounds.
    """
    await _configure(client)
    await _make_room(
        client, "Office", "sensor.office_temp", "cover.office_vent", schedule_target=68.0
    )

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 74.0, "temperature": None, "hvac_action": "idle"}
    )
    # 74 wants cooling to the 68 schedule target, but is inside the 62–78
    # envelope, so nothing may run.
    fake_ha.seed_state("sensor.office_temp", "74.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.office_vent", "open", {})
    await _enable_vacation(client)

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert logs == [], f"schedules must stay paused during vacation; got {logs}"


# ---------------------------------------------------------------------------
# The vent policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_breaching_rooms_share_the_air_then_close_near_their_bound(
    client, fake_ha, tick
) -> None:
    """The worked example: gym breaches, another room absorbs, then bows out.

    Gym 85°F over a 78°F ceiling starts a cooling cycle. A second room at 74°F
    is nowhere near the 62°F floor, so its vent stays OPEN and it shares the
    cooling — the pre-#619 engine would have closed it at cycle start. When it
    reaches 64°F (the 62°F floor + its 2°F deadband) it is within a deadband of
    its opposite bound and its vent closes, so it cannot overshoot into calling
    for heat while the gym is still recovering.
    """
    await _configure(client)
    await _make_room(client, "Gym", "sensor.gym_temp", "cover.gym_vent")
    await _make_room(client, "Study", "sensor.study_temp", "cover.study_vent")

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 80.0, "temperature": None, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.gym_temp", "85.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("sensor.study_temp", "74.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.gym_vent", "open", {})
    fake_ha.seed_state("cover.study_vent", "open", {})
    await _enable_vacation(client)

    await tick()

    assert _vent_state(fake_ha, "cover.study_vent") == "open", (
        "a non-breaching room well clear of its opposite bound must keep sharing the air"
    )

    # The study is driven down to its floor+deadband threshold.
    fake_ha.seed_state("sensor.study_temp", "64.0", {"unit_of_measurement": "°F"})
    await tick()

    assert _vent_state(fake_ha, "cover.study_vent") == "closed", (
        "at min_setpoint + deadband the room must stop absorbing air"
    )
    assert _vent_state(fake_ha, "cover.gym_vent") == "open", "the breaching room keeps cooling"

    events = await (await client.get("/api/logs/events?limit=50")).json()
    assert any("closed vents in 'Study'" in e["message"] for e in events), events


@pytest.mark.asyncio
async def test_room_without_a_readable_sensor_fails_open(client, fake_ha, tick) -> None:
    """No reading → keep the vent open.

    An unreadable room cannot be shown to be near its bound, and the hazard
    being guarded is dead-heading the air handler (#210), not overshoot.
    """
    await _configure(client)
    await _make_room(client, "Gym", "sensor.gym_temp", "cover.gym_vent")
    await _make_room(client, "Attic", None, "cover.attic_vent")

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 80.0, "temperature": None, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.gym_temp", "85.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.gym_vent", "open", {})
    fake_ha.seed_state("cover.attic_vent", "open", {})
    await _enable_vacation(client)

    await tick()

    assert _vent_state(fake_ha, "cover.attic_vent") == "open"


@pytest.mark.asyncio
async def test_airflow_floor_keeps_a_vent_open_when_every_room_is_satisfied(
    client, fake_ha, tick
) -> None:
    """The floor (#210/#213) outranks the vent policy.

    Two smart vents on a zone declaring two registers with a 1.0 open fraction:
    both must stay open. Even with the study fully satisfied, closing it would
    drop the zone below the floor, so it stays open and the air handler keeps
    its path.
    """
    await _configure(client, total_vents_count=2)
    await client.put(f"/api/thermostats/{THERMO}", json={"min_open_vents_fraction": 1.0})
    await _make_room(client, "Gym", "sensor.gym_temp", "cover.gym_vent")
    await _make_room(client, "Study", "sensor.study_temp", "cover.study_vent")

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 80.0, "temperature": None, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.gym_temp", "85.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("sensor.study_temp", "63.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.gym_vent", "open", {})
    fake_ha.seed_state("cover.study_vent", "open", {})
    await _enable_vacation(client)

    await tick()

    assert _vent_state(fake_ha, "cover.study_vent") == "open", (
        "the airflow floor must outrank the vacation vent policy"
    )


# ---------------------------------------------------------------------------
# Opt-out and the range-mode restriction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opting_out_restores_the_old_hold(client, fake_ha, tick) -> None:
    """`vacation_safety_cycles=False` → no cycle, hold commands the bound."""
    await _configure(client, vacation_safety_cycles=False)
    await _make_room(client, "Gym", "sensor.gym_temp", "cover.gym_vent")

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 85.0, "temperature": None, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.gym_temp", "85.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.gym_vent", "open", {})
    await _enable_vacation(client)

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert logs == [], "opting out must leave the pre-#619 hold in charge"
    cools = [
        c
        for c in fake_ha.calls_for("set_temperature")
        if c.data["entity_id"] == THERMO and c.data.get("hvac_mode") == "cool"
    ]
    assert cools and cools[-1].data["temperature"] == pytest.approx(78.0), (
        "the hold commands the bound itself, not a cycle setpoint"
    )


@pytest.mark.asyncio
async def test_range_mode_ignores_the_toggle_and_holds_the_range(client, fake_ha, tick) -> None:
    """Range mode never runs safety cycles even with the toggle ON.

    heat_cool hands direction to the equipment; the engine cannot lock a cycle
    mode it does not own (#26/#29). The restriction lives in
    `_vacation_safety_enabled`, so a room baking at 85°F still yields no cycle —
    only the native range hold.
    """
    await _configure(client, vacation_hvac_mode="range", vacation_safety_cycles=True)
    await _make_room(client, "Gym", "sensor.gym_temp", "cover.gym_vent")

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 85.0, "temperature": None, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.gym_temp", "85.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.gym_vent", "open", {})
    await _enable_vacation(client)

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert logs == [], "range mode must not run per-room safety cycles"
    ranges = [
        c
        for c in fake_ha.calls_for("set_temperature")
        if c.data["entity_id"] == THERMO and c.data.get("hvac_mode") == "heat_cool"
    ]
    assert ranges, f"the range hold must still run; calls={fake_ha.calls}"
    assert ranges[-1].data["target_temp_low"] == pytest.approx(62.0)
    assert ranges[-1].data["target_temp_high"] == pytest.approx(78.0)


@pytest.mark.asyncio
async def test_cycle_ends_and_the_hold_resumes_when_the_breach_clears(
    client, fake_ha, tick
) -> None:
    """Once the room is back inside the envelope the cycle ends and the hold
    takes the thermostat back — the two must never command in the same tick."""
    await _configure(client)
    await _make_room(client, "Gym", "sensor.gym_temp", "cover.gym_vent")

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 80.0, "temperature": None, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.gym_temp", "85.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.gym_vent", "open", {})
    await _enable_vacation(client)

    await tick()
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["ended_at"] is None

    # Room recovers inside the envelope; thermostat ambient follows it down.
    fake_ha.seed_state("sensor.gym_temp", "77.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(
        THERMO, "cool", {"current_temperature": 70.0, "temperature": 74.0, "hvac_action": "cooling"}
    )
    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is not None, "the cycle must close out"
    assert logs[0]["ended_reason"] == "aborted: vacation mode - envelope restored"
