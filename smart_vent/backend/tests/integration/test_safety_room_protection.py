"""Per-room safety protection integration tests (Issue #367).

The per-room ``max_setpoint`` / ``min_setpoint`` hard cap only protects rooms
that already have demand (presence/schedule/override). A room with no demand
source is never evaluated, so it can bake past the ceiling while a cycle runs
for other rooms — e.g. a presence cycle holds the Bedroom at 70°F while the
unoccupied Gym climbs to 78°F over a 77°F ceiling.

``_add_safety_rooms`` closes that gap: any zone room whose own sensor reading
has breached the envelope is pulled into the active set with ``source="safety"``
so the normal cycle machinery conditions it — joining the running cycle when
the direction matches, or starting a fresh one when the zone is idle. The
target is one deadband inside the breached bound, so the room is brought safely
back inside the envelope (with a hysteresis margin) rather than fully
conditioned like an occupied room.

These tests drive the full engine against a fake Home Assistant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"


async def _configure(
    client, *, min_setpoint: float = 62.0, max_setpoint: float = 77.0, deadband: float = 1.5
) -> None:
    await client.post(
        "/api/thermostats",
        json={
            "thermostat_entity_id": THERMO,
            "total_vents_count": 6,
            "min_setpoint": min_setpoint,
            "max_setpoint": max_setpoint,
            "deadband": deadband,
            "overshoot_delta": 2.0,
            "reconciliation_interval_min": 5,
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


async def _warnings(client) -> list[str]:
    events = await (await client.get("/api/logs/events?level=warning")).json()
    return [e["message"] for e in events]


@pytest.mark.asyncio
async def test_breaching_room_starts_protection_cycle_when_idle(client, fake_ha, tick) -> None:
    """Unoccupied room over the ceiling, zone idle → a protection cooling cycle.

    Nobody home, no schedule — but the Gym sensor reads 78°F, 1°F over the
    77°F ceiling. The room is pulled into a fresh cooling cycle for protection.
    """
    await _configure(client)
    gym = await _make_room(client, "Gym", "sensor.gym_temp", "cover.gym_vent")

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 78.0, "temperature": None, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.gym_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.gym_vent", "open", {})

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1, f"a protection cooling cycle should start; got {logs}"
    assert logs[0]["mode"] == "cooling"
    gym_meta = logs[0]["rooms"][gym]
    assert gym_meta["source"] == "safety"
    # Target = one deadband inside the ceiling: 77 − 1.5 = 75.5°F.
    assert gym_meta["target"] == pytest.approx(75.5)

    sp_calls = fake_ha.calls_for("set_temperature")
    assert sp_calls and sp_calls[-1].data.get("hvac_mode") == "cool", fake_ha.calls

    warnings = await _warnings(client)
    assert any("Safety protection engaged for room 'Gym'" in m for m in warnings), warnings


@pytest.mark.asyncio
async def test_breaching_room_joins_running_presence_cycle(client, fake_ha, tick) -> None:
    """The reported scenario: Bedroom cooling for an occupant, Gym then bakes.

    A cooling cycle is already running to hold the occupied Bedroom at 70°F.
    The unoccupied Gym drifts to 78°F (over the 77°F ceiling) and must be pulled
    into that SAME cycle for protection — not left to bake, and not spun up as a
    competing second cycle.
    """
    await _configure(client)
    bedroom = await _make_room(
        client, "Bedroom", "sensor.bedroom_temp", "cover.bedroom_vent", schedule_target=70.0
    )
    gym = await _make_room(client, "Gym", "sensor.gym_temp", "cover.gym_vent")

    # Warm upstairs. Bedroom (occupied, target 70) drives cooling; Gym in-band.
    fake_ha.seed_state(
        THERMO, "cool", {"current_temperature": 78.0, "temperature": 78.0, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.bedroom_temp", "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("sensor.gym_temp", "72.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.bedroom_vent", "open", {})
    fake_ha.seed_state("cover.gym_vent", "open", {})

    await tick()
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["mode"] == "cooling"
    cycle_id = logs[0]["id"]
    assert gym not in logs[0]["rooms"], "in-band Gym should not be in the cycle yet"

    # Gym now bakes to 78°F — over the 77°F ceiling — with nobody in it.
    fake_ha.seed_state("sensor.gym_temp", "78.0", {"unit_of_measurement": "°F"})
    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1, "Gym should JOIN the running cycle, not start a second"
    assert logs[0]["id"] == cycle_id
    assert logs[0]["rooms"][gym]["source"] == "safety"
    assert logs[0]["rooms"][gym]["target"] == pytest.approx(75.5)
    assert logs[0]["rooms"][bedroom]["source"] == "schedule"

    warnings = await _warnings(client)
    assert any("Safety protection engaged for room 'Gym'" in m for m in warnings), warnings


@pytest.mark.asyncio
async def test_room_below_floor_starts_heating_protection(client, fake_ha, tick) -> None:
    """Unoccupied room under the floor → a heating protection cycle (freeze guard)."""
    await _configure(client)
    gym = await _make_room(client, "Gym", "sensor.gym_temp", "cover.gym_vent")

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 55.0, "temperature": None, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.gym_temp", "55.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.gym_vent", "open", {})

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["mode"] == "heating", logs
    assert logs[0]["rooms"][gym]["source"] == "safety"
    # Target = one deadband inside the floor: 62 + 1.5 = 63.5°F.
    assert logs[0]["rooms"][gym]["target"] == pytest.approx(63.5)

    sp_calls = fake_ha.calls_for("set_temperature")
    assert sp_calls and sp_calls[-1].data.get("hvac_mode") == "heat", fake_ha.calls


@pytest.mark.asyncio
async def test_in_envelope_room_not_activated(client, fake_ha, tick) -> None:
    """A room comfortably inside the envelope is never safety-activated."""
    await _configure(client)
    await _make_room(client, "Gym", "sensor.gym_temp", "cover.gym_vent")

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 72.0, "temperature": None, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.gym_temp", "72.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.gym_vent", "open", {})

    fake_ha.reset_calls()
    await tick()

    assert (await (await client.get("/api/logs")).json()) == [], "in-band room must not activate"
    warnings = await _warnings(client)
    assert not any("Safety protection" in m for m in warnings), warnings
    # #433 zero-diff property: the always-on safety feature must produce ZERO
    # HA traffic when nothing breaches — no setpoint writes, no cover
    # commands. A regression here changes behavior for every install.
    assert not fake_ha.calls_for("set_temperature"), "no-breach tick must send no setpoints"
    assert not fake_ha.calls_for("close_cover") and not fake_ha.calls_for("open_cover"), (
        "no-breach tick must send no cover commands"
    )


@pytest.mark.asyncio
async def test_protection_warns_once_per_episode(client, fake_ha, tick) -> None:
    """The activation warning fires once per breach, not every tick it persists."""
    await _configure(client)
    await _make_room(client, "Gym", "sensor.gym_temp", "cover.gym_vent")

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 78.0, "temperature": None, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.gym_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.gym_vent", "open", {})

    await tick()
    await tick()  # still breaching — must NOT re-announce

    warnings = await _warnings(client)
    engaged = [m for m in warnings if "Safety protection engaged for room 'Gym'" in m]
    assert len(engaged) == 1, f"expected exactly one activation warning; got {engaged}"


@pytest.mark.asyncio
async def test_room_without_sensor_not_activated(client, fake_ha, tick) -> None:
    """A room with no readable sensor can't be safety-activated (no false cycle).

    Per-room protection needs a room temperature; a sensorless zone is the
    thermostat-ambient backstop's job, not this path. With the thermostat in
    band too, nothing should happen.
    """
    await _configure(client)
    resp = await client.post("/api/rooms", json={"name": "Gym", "thermostat_entity_id": THERMO})
    gym = (await resp.json())["id"]
    await client.post(
        f"/api/rooms/{gym}/vents",
        json={"entity_id": "cover.gym_vent", "control_method": "open_close"},
    )

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 70.0, "temperature": None, "hvac_action": "idle"}
    )
    fake_ha.seed_state("cover.gym_vent", "open", {})

    await tick()

    assert (await (await client.get("/api/logs")).json()) == [], "sensorless room can't activate"


# ---------------------------------------------------------------------------
# The safety sweep and the per-schedule deadband (Issue #517)
#
# `_add_safety_rooms` calls `_effective_deadband(room, tc.deadband)` with NO
# schedule band, deliberately: the loop skips every room already in
# `new_active_map`, and a room with a MATCHING enabled block is active by
# construction — so a room that reaches the sweep cannot have one. The chain
# there is room→thermostat, full stop.
# ---------------------------------------------------------------------------


async def _park_banded_block(client, room_id: str, band: float) -> None:
    """Attach a DISABLED block carrying a wide band. It can never match, so it
    must never influence the safety target."""
    resp = await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={
            "days_of_week": list(range(7)),
            "start_time": "00:00",
            "end_time": "23:59",
            "target_temp": 70.0,
            "enabled": False,
            "deadband_override": band,
        },
    )
    assert resp.status == 201, await resp.text()


@pytest.mark.asyncio
async def test_safety_target_ignores_a_schedule_band_cooling(client, fake_ha, tick) -> None:
    """Ceiling breach: the safety target is one THERMOSTAT deadband inside the
    max setpoint (77 − 1.5 = 75.5), never one block band inside it (77 − 8)."""
    await _configure(client)  # max_setpoint 77, deadband 1.5
    gym = await _make_room(client, "Gym", "sensor.gym_temp", "cover.gym_vent")
    await _park_banded_block(client, gym, 8.0)

    fake_ha.seed_state(
        THERMO, "cool", {"current_temperature": 80.0, "temperature": 80.0, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.gym_temp", "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.gym_vent", "open", {})

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["mode"] == "cooling", logs
    assert logs[0]["rooms"][gym]["source"] == "safety"
    assert logs[0]["rooms"][gym]["target"] == pytest.approx(75.5), (
        "the safety target must use the thermostat deadband, not a schedule band"
    )


@pytest.mark.asyncio
async def test_safety_target_ignores_a_schedule_band_heating(client, fake_ha, tick) -> None:
    """Floor breach mirror: 62 + 1.5 = 63.5, never 62 + 8."""
    await _configure(client)  # min_setpoint 62, deadband 1.5
    gym = await _make_room(client, "Gym", "sensor.gym_temp", "cover.gym_vent")
    await _park_banded_block(client, gym, 8.0)

    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 55.0, "temperature": None, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.gym_temp", "55.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.gym_vent", "open", {})

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["mode"] == "heating", logs
    assert logs[0]["rooms"][gym]["target"] == pytest.approx(63.5)


@pytest.mark.asyncio
async def test_safety_target_still_honours_the_room_band(client, fake_ha, tick) -> None:
    """The contrast case that proves the sweep resolves room→thermostat rather
    than "always the thermostat": a per-room override (#277) DOES move the
    safety target (77 − 3 = 74), even with a parked block asking for 8."""
    await _configure(client)
    gym = await _make_room(client, "Gym", "sensor.gym_temp", "cover.gym_vent")
    await client.put(f"/api/rooms/{gym}", json={"deadband_override": 3.0})
    await _park_banded_block(client, gym, 8.0)

    fake_ha.seed_state(
        THERMO, "cool", {"current_temperature": 80.0, "temperature": 80.0, "hvac_action": "idle"}
    )
    fake_ha.seed_state("sensor.gym_temp", "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.gym_vent", "open", {})

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1, logs
    assert logs[0]["rooms"][gym]["target"] == pytest.approx(74.0)
