"""Per-control-method vent dispatch coverage.

Each ``control_method`` routes to a different HA service
(``vent_controller.py:_invoke_open``/``_invoke_close``). These tests run
a real cycle end-to-end for every method and assert that the correct HA
service was recorded on the fake client.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


async def _make_room_with_vent(client, control_method: str) -> str:
    """Create a room + sensor + vent (with the requested control_method)
    + a schedule covering "now". Returns the room id."""
    resp = await client.post(
        "/api/rooms",
        json={"name": "Bedroom", "thermostat_entity_id": "climate.test_thermostat"},
    )
    room_id: str = (await resp.json())["id"]

    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.test_room_temp"})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": "cover.test_room_vent", "control_method": control_method},
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
            "target_temp": 72.0,
        },
    )
    return room_id


def _seed_cooling_world(fake_ha, vent_state: str = "closed") -> None:
    """Warm room + cool mode → engine should try to open the vent."""
    fake_ha.seed_state(
        "climate.test_thermostat",
        "cool",
        {"current_temperature": 78.0, "temperature": 76.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state("sensor.test_room_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.test_room_vent", vent_state, {})


async def _drive_to_close(client, fake_ha, tick) -> None:
    """First tick starts the cooling cycle (vent opens, setpoint written);
    then the room hits target and a second tick should close the vent."""
    fake_ha.seed_state(
        "climate.test_thermostat",
        "cool",
        {"current_temperature": 78.0, "temperature": 76.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state("sensor.test_room_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.test_room_vent", "closed", {})
    await tick()
    # Room now at target → next tick closes vents.
    await fake_ha.set_entity_state("sensor.test_room_temp", "72.0", {"unit_of_measurement": "°F"})
    fake_ha.reset_calls()
    await tick()


# ---------------------------------------------------------------------------
# open_close: open_cover / close_cover
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_close_vent_uses_open_cover_on_cycle_start(client, fake_ha, tick) -> None:
    _seed_cooling_world(fake_ha, vent_state="closed")
    await _make_room_with_vent(client, "open_close")

    await tick()

    opens = fake_ha.calls_for("open_cover")
    assert opens, f"expected open_cover; got {fake_ha.calls}"
    assert opens[0].data["entity_id"] == "cover.test_room_vent"
    # Different methods must NOT be used for this vent.
    assert not fake_ha.calls_for("set_cover_position")
    assert not fake_ha.calls_for("set_cover_tilt_position")
    assert not fake_ha.calls_for("toggle")


@pytest.mark.asyncio
async def test_open_close_vent_uses_close_cover_on_reaching_target(client, fake_ha, tick) -> None:
    await _make_room_with_vent(client, "open_close")
    await _drive_to_close(client, fake_ha, tick)

    closes = fake_ha.calls_for("close_cover")
    assert closes, f"expected close_cover; got {fake_ha.calls}"
    assert closes[0].data["entity_id"] == "cover.test_room_vent"


# ---------------------------------------------------------------------------
# set_position: set_cover_position(100/0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_position_vent_writes_100_on_open(client, fake_ha, tick) -> None:
    _seed_cooling_world(fake_ha, vent_state="closed")
    await _make_room_with_vent(client, "set_position")

    await tick()

    pos_calls = fake_ha.calls_for("set_cover_position")
    assert pos_calls, f"expected set_cover_position; got {fake_ha.calls}"
    assert pos_calls[0].data == {"entity_id": "cover.test_room_vent", "position": 100}
    assert not fake_ha.calls_for("open_cover")


@pytest.mark.asyncio
async def test_set_position_vent_writes_0_on_close(client, fake_ha, tick) -> None:
    await _make_room_with_vent(client, "set_position")
    await _drive_to_close(client, fake_ha, tick)

    pos_calls = fake_ha.calls_for("set_cover_position")
    positions = [c.data["position"] for c in pos_calls]
    # Hitting target closes the vent (position=0). After the cycle completes,
    # the engine also re-opens all zone vents (position=100).
    assert 0 in positions, f"no close-position (0) call: {pos_calls}"
    assert not fake_ha.calls_for("close_cover")


# ---------------------------------------------------------------------------
# set_tilt_position: set_cover_tilt_position(100/0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_tilt_position_vent_writes_100_on_open(client, fake_ha, tick) -> None:
    _seed_cooling_world(fake_ha, vent_state="closed")
    await _make_room_with_vent(client, "set_tilt_position")

    await tick()

    tilt_calls = fake_ha.calls_for("set_cover_tilt_position")
    assert tilt_calls, f"expected set_cover_tilt_position; got {fake_ha.calls}"
    assert tilt_calls[0].data == {"entity_id": "cover.test_room_vent", "tilt_position": 100}
    assert not fake_ha.calls_for("open_cover")


@pytest.mark.asyncio
async def test_set_tilt_position_vent_writes_0_on_close(client, fake_ha, tick) -> None:
    await _make_room_with_vent(client, "set_tilt_position")
    await _drive_to_close(client, fake_ha, tick)

    tilt_calls = fake_ha.calls_for("set_cover_tilt_position")
    tilts = [c.data["tilt_position"] for c in tilt_calls]
    assert 0 in tilts, f"no close-tilt (0) call: {tilt_calls}"
    assert not fake_ha.calls_for("close_cover")


# ---------------------------------------------------------------------------
# toggle: cover.toggle for both directions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toggle_vent_uses_cover_toggle_on_open(client, fake_ha, tick) -> None:
    _seed_cooling_world(fake_ha, vent_state="closed")
    await _make_room_with_vent(client, "toggle")

    await tick()

    toggles = fake_ha.calls_for("toggle")
    assert toggles, f"expected cover.toggle; got {fake_ha.calls}"
    assert toggles[0].data == {"entity_id": "cover.test_room_vent"}
    assert toggles[0].domain == "cover"
    assert not fake_ha.calls_for("open_cover")
    assert not fake_ha.calls_for("set_cover_position")
    assert not fake_ha.calls_for("set_cover_tilt_position")


@pytest.mark.asyncio
async def test_toggle_vent_uses_cover_toggle_on_close(client, fake_ha, tick) -> None:
    await _make_room_with_vent(client, "toggle")
    await _drive_to_close(client, fake_ha, tick)

    toggles = fake_ha.calls_for("toggle")
    assert toggles, f"expected cover.toggle; got {fake_ha.calls}"
    assert toggles[-1].data == {"entity_id": "cover.test_room_vent"}
    assert not fake_ha.calls_for("close_cover")
