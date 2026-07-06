"""Engine-level guards for non-open_close vents (#424, #425).

Two failure modes of treating every vent like an ``open_close`` cover:

  - a ``toggle`` vent that is already closed receives ``cover.toggle`` from a
    direct close path and swings OPEN (#424) — the idle-room close at cycle
    start was one such path;
  - a ``set_tilt_position`` vent whose HA ``state`` reads "open" while
    tilt-closed is skipped by the "already open" check, so cycle start never
    actually opens it (#425).

These drive full scheduler ticks to pin the guards through the real engine
paths, not just the VentController unit layer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"


async def _make_room(client, name: str, sensor: str | None, vent: str, control_method: str) -> str:
    resp = await client.post("/api/rooms", json={"name": name, "thermostat_entity_id": THERMO})
    room_id: str = (await resp.json())["id"]
    if sensor:
        await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": sensor})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": vent, "control_method": control_method},
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


@pytest.mark.asyncio
async def test_idle_room_close_does_not_toggle_a_closed_toggle_vent(client, fake_ha, tick) -> None:
    """Cycle start closes idle rooms' vents. A toggle vent already closed must
    be left alone — toggling it would OPEN it while the log claims closure."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 80.0, "temperature": 78.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state("sensor.room_a", "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.room_a_vent", "open", {})
    # Idle room's toggle vent, already physically closed.
    fake_ha.seed_state("cover.room_b_vent", "closed", {})

    room_a = await _make_room(client, "RoomA", "sensor.room_a", "cover.room_a_vent", "open_close")
    await _add_all_day_schedule(client, room_a, 72.0)
    await _make_room(client, "RoomB", None, "cover.room_b_vent", "toggle")

    await tick()  # cooling cycle starts for A; idle-room close runs for B

    assert client.app["scheduler"]._engines[THERMO].cycle_state.value == "running"
    toggles = [c for c in fake_ha.calls_for("toggle") if c.data["entity_id"] == "cover.room_b_vent"]
    assert not toggles, "a closed toggle vent must not be toggled (it would open)"
    assert fake_ha.get_state("cover.room_b_vent")["state"] == "closed"


@pytest.mark.asyncio
async def test_cycle_start_opens_tilt_vent_whose_state_lies_open(client, fake_ha, tick) -> None:
    """A tilt vent reporting state='open' while tilt-closed must be driven to
    tilt 100 at cycle start — HA's cover state does not track tilt."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 80.0, "temperature": 78.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state("sensor.room_a", "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.tilt_vent", "open", {"current_tilt_position": 0})

    room_a = await _make_room(
        client, "RoomA", "sensor.room_a", "cover.tilt_vent", "set_tilt_position"
    )
    await _add_all_day_schedule(client, room_a, 72.0)

    await tick()  # cycle starts — the active room's vent must actually open

    tilt_calls = [
        c
        for c in fake_ha.calls_for("set_cover_tilt_position")
        if c.data["entity_id"] == "cover.tilt_vent"
    ]
    assert tilt_calls, "cycle start must command the tilt vent open despite state='open'"
    assert tilt_calls[-1].data["tilt_position"] == 100


@pytest.mark.asyncio
async def test_room_at_target_closes_tilt_vent_whose_state_lies_closed(
    client, fake_ha, tick
) -> None:
    """The mirror of the open-side lie: a tilt vent physically open (tilt 100)
    whose HA state reads 'closed' must still receive tilt→0 when its room
    reaches target — the state-only skip left it blowing air forever while
    the cycle bookkeeping said closed."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 80.0, "temperature": 78.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state("sensor.room_a", "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("sensor.room_b", "80.0", {"unit_of_measurement": "°F"})
    # Physically open (tilt 100) but state lies "closed".
    fake_ha.seed_state("cover.tilt_vent", "closed", {"current_tilt_position": 100})
    fake_ha.seed_state("cover.room_b_vent", "open", {})

    room_a = await _make_room(
        client, "RoomA", "sensor.room_a", "cover.tilt_vent", "set_tilt_position"
    )
    await _add_all_day_schedule(client, room_a, 72.0)
    # Second active room keeps the cycle alive after A closes, so the close
    # goes through the normal (non-bypass) close_room_vents path.
    room_b = await _make_room(client, "RoomB", "sensor.room_b", "cover.room_b_vent", "open_close")
    await _add_all_day_schedule(client, room_b, 72.0)

    await tick()  # cooling cycle starts for A+B

    # Room A reaches target — its tilt vent must be commanded to 0 despite
    # state="closed".
    await fake_ha.set_entity_state("sensor.room_a", "72.0", {"unit_of_measurement": "°F"})
    await tick()

    tilt_closes = [
        c
        for c in fake_ha.calls_for("set_cover_tilt_position")
        if c.data["entity_id"] == "cover.tilt_vent" and c.data["tilt_position"] == 0
    ]
    assert tilt_closes, (
        "room at target must command tilt→0 even when HA state already says 'closed'"
    )
