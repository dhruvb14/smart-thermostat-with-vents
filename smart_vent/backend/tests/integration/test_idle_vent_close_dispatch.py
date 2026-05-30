"""
Regression test for issue #82.

``_close_idle_room_vents`` and the mid-cycle "removed" path used to call
``self._ha.close_cover(entity_id)`` directly, bypassing
``VentController._invoke_close``. For vents configured with
``control_method`` ∈ {``set_position``, ``set_tilt_position``, ``toggle``}
the ``cover.close_cover`` service is a no-op, so idle-room vents silently
stayed open even though the engine logged "Closed idle room X vents".

These tests verify that the idle-close path now dispatches to the correct
HA service for each control method.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


async def _create_room_with_schedule(
    client,
    *,
    name: str,
    sensor_entity: str,
    vent_entity: str,
    control_method: str,
    sensor_temp: float,
) -> str:
    resp = await client.post(
        "/api/rooms",
        json={"name": name, "thermostat_entity_id": "climate.test_thermostat"},
    )
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": sensor_entity})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": vent_entity, "control_method": control_method},
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


async def _create_idle_room(
    client,
    *,
    name: str,
    sensor_entity: str,
    vent_entity: str,
    control_method: str,
) -> str:
    """Idle room: sensor + vent but no schedule and no presence."""
    resp = await client.post(
        "/api/rooms",
        json={"name": name, "thermostat_entity_id": "climate.test_thermostat"},
    )
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": sensor_entity})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": vent_entity, "control_method": control_method},
    )
    return room_id


@pytest.mark.asyncio
async def test_idle_vent_closed_via_set_position(client, fake_ha, tick) -> None:
    """Idle room with ``control_method=set_position`` must be closed with
    ``set_cover_position(position=0)``, not ``close_cover`` (issue #82)."""
    thermostat = "climate.test_thermostat"
    fake_ha.seed_state(
        thermostat,
        "cool",
        {"current_temperature": 76.0, "temperature": 76.0, "hvac_action": "idle"},
    )
    # Active room: warm, needs cooling.
    fake_ha.seed_state("sensor.active_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(
        "cover.active_vent",
        "open",
        {"current_position": 100},
    )
    # Idle room: already at target. Vent starts OPEN at position 100 (as if a
    # prior _terminate_cycle reopened everything) and uses set_position.
    fake_ha.seed_state("sensor.idle_temp", "72.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(
        "cover.idle_vent",
        "open",
        {"current_position": 100},
    )

    await _create_room_with_schedule(
        client,
        name="Active",
        sensor_entity="sensor.active_temp",
        vent_entity="cover.active_vent",
        control_method="set_position",
        sensor_temp=78.0,
    )
    await _create_idle_room(
        client,
        name="Idle",
        sensor_entity="sensor.idle_temp",
        vent_entity="cover.idle_vent",
        control_method="set_position",
    )

    await tick()

    # With the bug present, engine calls cover.close_cover which is a no-op
    # on a position-based cover. The fix routes through VentController, which
    # calls cover.set_cover_position with position=0 for set_position vents.
    close_calls = fake_ha.calls_for("close_cover")
    idle_vent_closed_via_close = any(
        c.data.get("entity_id") == "cover.idle_vent" for c in close_calls
    )
    assert not idle_vent_closed_via_close, (
        f"idle vent must NOT be closed via cover.close_cover for a set_position "
        f"control_method; calls: {close_calls}"
    )

    position_calls = fake_ha.calls_for("set_cover_position")
    idle_vent_set_to_zero = [
        c
        for c in position_calls
        if c.data.get("entity_id") == "cover.idle_vent" and c.data.get("position") == 0
    ]
    assert idle_vent_set_to_zero, (
        f"idle vent should have been closed via set_cover_position(position=0); "
        f"all calls: {fake_ha.calls}"
    )

    idle_state = fake_ha.get_state("cover.idle_vent")
    assert idle_state is not None and idle_state["state"] == "closed", (
        f"idle vent state should be 'closed' after set_position(0); got {idle_state}"
    )


@pytest.mark.asyncio
async def test_idle_vent_closed_via_set_tilt_position(client, fake_ha, tick) -> None:
    """Tilt-based covers must be closed with ``set_cover_tilt_position``."""
    thermostat = "climate.test_thermostat"
    fake_ha.seed_state(
        thermostat,
        "cool",
        {"current_temperature": 76.0, "temperature": 76.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state("sensor.active_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(
        "cover.active_vent",
        "open",
        {"current_tilt_position": 100},
    )
    fake_ha.seed_state("sensor.idle_temp", "72.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(
        "cover.idle_vent",
        "open",
        {"current_tilt_position": 100},
    )

    await _create_room_with_schedule(
        client,
        name="Active",
        sensor_entity="sensor.active_temp",
        vent_entity="cover.active_vent",
        control_method="set_tilt_position",
        sensor_temp=78.0,
    )
    await _create_idle_room(
        client,
        name="Idle",
        sensor_entity="sensor.idle_temp",
        vent_entity="cover.idle_vent",
        control_method="set_tilt_position",
    )

    await tick()

    tilt_calls = fake_ha.calls_for("set_cover_tilt_position")
    idle_tilt_to_zero = [
        c
        for c in tilt_calls
        if c.data.get("entity_id") == "cover.idle_vent" and c.data.get("tilt_position") == 0
    ]
    assert idle_tilt_to_zero, (
        f"idle vent should have been closed via set_cover_tilt_position(tilt_position=0); "
        f"all calls: {fake_ha.calls}"
    )


@pytest.mark.asyncio
async def test_reconcile_recloses_drifted_idle_vent(client, fake_ha, tick) -> None:
    """If an idle-room vent is externally reopened mid-cycle, the reconciler
    must detect the drift and re-close it (issue #82, secondary bug)."""
    thermostat = "climate.test_thermostat"
    fake_ha.seed_state(
        thermostat,
        "cool",
        {"current_temperature": 76.0, "temperature": 76.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state("sensor.active_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.active_vent", "open", {})
    fake_ha.seed_state("sensor.idle_temp", "72.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.idle_vent", "open", {})

    # Enable the reconciler — it's off by default (interval=0 disables).
    await client.post(
        "/api/thermostats",
        json={
            "thermostat_entity_id": thermostat,
            "reconciliation_interval_min": 1,  # fire reconcile every tick
            "total_vents_count": 6,
        },
    )

    await _create_room_with_schedule(
        client,
        name="Active",
        sensor_entity="sensor.active_temp",
        vent_entity="cover.active_vent",
        control_method="open_close",
        sensor_temp=78.0,
    )
    await _create_idle_room(
        client,
        name="Idle",
        sensor_entity="sensor.idle_temp",
        vent_entity="cover.idle_vent",
        control_method="open_close",
    )

    # Start the cycle — idle vent closes on fresh start.
    await tick()
    assert fake_ha.get_state("cover.idle_vent")["state"] == "closed", (
        "precondition: idle vent should be closed after initial cycle start"
    )

    # External actor re-opens the idle vent mid-cycle.
    await fake_ha.set_entity_state("cover.idle_vent", "open", {})
    fake_ha.reset_calls()

    # Force the reconciler to run on the next tick by clearing the last-run
    # timestamp on the engine. (_maybe_reconcile gates on interval.)
    scheduler = client.app["scheduler"]
    engine = scheduler._engines[thermostat]
    engine._last_reconciled_at = None

    await tick()

    close_calls = fake_ha.calls_for("close_cover")
    closed_entities = {c.data.get("entity_id") for c in close_calls}
    assert "cover.idle_vent" in closed_entities, (
        f"reconciler must re-close drifted idle vent; close calls: {close_calls}"
    )
    assert fake_ha.get_state("cover.idle_vent")["state"] == "closed"


@pytest.mark.asyncio
async def test_terminate_reopens_idle_room_vents(client, fake_ha, tick) -> None:
    """Cycle termination must reopen idle-room vents that were closed at cycle
    start, not just the active-room vents (issue #244)."""
    thermostat = "climate.test_thermostat"
    fake_ha.seed_state(
        thermostat,
        "cool",
        {"current_temperature": 76.0, "temperature": 76.0, "hvac_action": "cooling"},
    )
    # Active room: warm → will drive a cooling cycle.
    fake_ha.seed_state("sensor.active_temp", "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.active_vent", "open", {})
    # Idle room: already comfortable → vent will be closed at cycle start.
    fake_ha.seed_state("sensor.idle_temp", "72.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.idle_vent", "open", {})

    await _create_room_with_schedule(
        client,
        name="Active",
        sensor_entity="sensor.active_temp",
        vent_entity="cover.active_vent",
        control_method="open_close",
        sensor_temp=78.0,
    )
    await _create_idle_room(
        client,
        name="Idle",
        sensor_entity="sensor.idle_temp",
        vent_entity="cover.idle_vent",
        control_method="open_close",
    )

    # Tick 1: cycle starts, idle vent closes.
    await tick()
    assert fake_ha.get_state("cover.idle_vent")["state"] == "closed", (
        "precondition: idle vent should be closed after cycle start"
    )
    assert fake_ha.get_state("cover.active_vent")["state"] == "open", (
        "precondition: active vent should be open"
    )

    # Drive active room to target so the cycle terminates on the next tick.
    await fake_ha.set_entity_state("sensor.active_temp", "72.0", {"unit_of_measurement": "°F"})
    fake_ha.reset_calls()

    # Tick 2: active room hits target → cycle terminates → all zone vents re-open.
    await tick()

    open_calls = fake_ha.calls_for("open_cover")
    opened_entities = {c.data.get("entity_id") for c in open_calls}
    assert "cover.idle_vent" in opened_entities, (
        f"_terminate_cycle must reopen idle-room vents (issue #244); "
        f"open_cover calls: {open_calls}, all calls: {fake_ha.calls}"
    )
    assert fake_ha.get_state("cover.idle_vent")["state"] == "open", (
        "idle vent must be open after cycle termination"
    )
