"""Eco Mode integration tests (Issue #404).

Drives the full engine against a fake Home Assistant to verify:

  - a hot-outdoor cooling cycle has its target relaxed, the setpoint reflects
    the relaxed (effective) target, and the cycle records requested/effective/
    eco_active;
  - with Eco OFF the setpoint and records are byte-identical to the pre-feature
    behaviour (effective == requested, eco_active False);
  - a room can enable Eco even when its thermostat has it off;
  - the write boundary converts thermostat + room Eco fields (°F and °C) and the
    room fields are nullable (null = inherit);
  - the eco_impact metric (per-thermostat + home) and the Logs pill flag.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"
OUTDOOR = "sensor.outdoor_temp"


async def _create_cooling_room(client, target_temp: float = 70.0) -> str:
    resp = await client.post("/api/rooms", json={"name": "Bedroom", "thermostat_entity_id": THERMO})
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.test_room_temp"})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": "cover.test_room_vent", "control_method": "open_close"},
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


def _seed_warm_room(fake_ha, room_temp: float = 78.0) -> None:
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 78.0, "temperature": 76.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state("sensor.test_room_temp", str(room_temp), {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.test_room_vent", "open", {})


async def _configure_outdoor(client, fake_ha, temp_f: float) -> None:
    fake_ha.seed_state(OUTDOOR, str(temp_f), {"unit_of_measurement": "°F"})
    await client.put("/api/settings/outside-temp-entity", json={"entity_id": OUTDOOR})


# A degenerate "step" Eco config (full_drift == threshold) makes the relaxed
# target deterministic: any outdoor >= 86 °F relaxes the 70 °F cooling target by
# the full 4 °F to exactly 74 °F.
_STEP_ECO = {
    "eco_mode_enabled": True,
    "eco_cooling_outdoor_threshold": 86,
    "eco_cooling_full_drift_temp": 86,
    "eco_cooling_max_drift": 4,
}


@pytest.mark.asyncio
async def test_cooling_target_relaxed_when_hot_outside(client, fake_ha, tick) -> None:
    _seed_warm_room(fake_ha)
    await _configure_outdoor(client, fake_ha, 95.0)
    await _create_cooling_room(client, target_temp=70.0)
    assert (await client.put(f"/api/thermostats/{THERMO}", json=_STEP_ECO)).status == 200

    await tick()

    # Setpoint reflects the relaxed 74 °F target minus the 2 °F overshoot.
    sp = fake_ha.calls_for("set_temperature")[-1].data["temperature"]
    assert sp == pytest.approx(72.0), f"expected relaxed setpoint 72, got {sp}"

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["mode"] == "cooling"
    assert logs[0]["eco_active"] is True  # Logs pill flag

    detail = await (await client.get(f"/api/logs/{logs[0]['id']}/detail")).json()
    room = detail["rooms"][0]
    assert room["requested_target"] == pytest.approx(70.0)
    assert room["effective_target"] == pytest.approx(74.0)
    assert room["eco_active"] is True
    assert detail["cycle"]["eco_active"] is True


@pytest.mark.asyncio
async def test_eco_off_is_byte_identical(client, fake_ha, tick) -> None:
    """Eco OFF (default): the setpoint and records match the pre-feature path."""
    _seed_warm_room(fake_ha)
    await _configure_outdoor(client, fake_ha, 95.0)
    await _create_cooling_room(client, target_temp=70.0)
    # Configure the thermostat row but leave Eco disabled.
    await client.put(f"/api/thermostats/{THERMO}", json={"name": "Test"})

    await tick()

    sp = fake_ha.calls_for("set_temperature")[-1].data["temperature"]
    assert sp == pytest.approx(68.0), "unrelaxed setpoint = 70 target − 2 overshoot"

    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["eco_active"] is False
    detail = await (await client.get(f"/api/logs/{logs[0]['id']}/detail")).json()
    room = detail["rooms"][0]
    assert room["requested_target"] == pytest.approx(70.0)
    assert room["effective_target"] == pytest.approx(70.0)
    assert room["eco_active"] is False


@pytest.mark.asyncio
async def test_below_threshold_no_relaxation(client, fake_ha, tick) -> None:
    _seed_warm_room(fake_ha)
    await _configure_outdoor(client, fake_ha, 80.0)  # below the 86 threshold
    await _create_cooling_room(client, target_temp=70.0)
    await client.put(f"/api/thermostats/{THERMO}", json=_STEP_ECO)

    await tick()

    sp = fake_ha.calls_for("set_temperature")[-1].data["temperature"]
    assert sp == pytest.approx(68.0)
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["eco_active"] is False


@pytest.mark.asyncio
async def test_eco_noop_when_sensor_unreadable(client, fake_ha, tick) -> None:
    """Eco enabled but the outdoor sensor is unreadable → relaxation stays inert
    (the engine reads no outdoor temperature, so it takes the unchanged path)."""
    _seed_warm_room(fake_ha)
    await _configure_outdoor(client, fake_ha, 95.0)  # configured → enable allowed
    await _create_cooling_room(client, target_temp=70.0)
    await client.put(f"/api/thermostats/{THERMO}", json=_STEP_ECO)
    # Sensor goes unavailable before the tick → no outdoor reading.
    fake_ha.seed_state(OUTDOOR, "unavailable", {})

    await tick()

    sp = fake_ha.calls_for("set_temperature")[-1].data["temperature"]
    assert sp == pytest.approx(68.0)


@pytest.mark.asyncio
async def test_room_enables_eco_when_thermostat_off(client, fake_ha, tick) -> None:
    _seed_warm_room(fake_ha)
    await _configure_outdoor(client, fake_ha, 95.0)
    room_id = await _create_cooling_room(client, target_temp=70.0)
    # Thermostat Eco OFF, but it still carries the numeric config (defaults).
    await client.put(f"/api/thermostats/{THERMO}", json={"eco_mode_enabled": False})
    # Room opts in and pins the step config so the drift is deterministic.
    resp = await client.put(
        f"/api/rooms/{room_id}",
        json={
            "eco_mode_enabled": True,
            "eco_cooling_outdoor_threshold": 86,
            "eco_cooling_full_drift_temp": 86,
            "eco_cooling_max_drift": 4,
        },
    )
    assert resp.status == 200

    await tick()

    sp = fake_ha.calls_for("set_temperature")[-1].data["temperature"]
    assert sp == pytest.approx(72.0), "room-level enable overrides thermostat-off"


@pytest.mark.asyncio
async def test_thermostat_eco_config_roundtrips_fahrenheit(client, fake_ha) -> None:
    await _configure_outdoor(client, fake_ha, 80.0)  # required to enable Eco
    resp = await client.put(
        f"/api/thermostats/{THERMO}",
        json={
            "eco_mode_enabled": True,
            "eco_cooling_outdoor_threshold": 86,
            "eco_cooling_max_drift": 4,
            "eco_hysteresis_band": 2,
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["eco_mode_enabled"] is True
    assert body["eco_cooling_outdoor_threshold"] == 86.0
    assert body["eco_cooling_max_drift"] == 4.0
    assert body["eco_hysteresis_band"] == 2.0


@pytest.mark.asyncio
async def test_thermostat_eco_config_roundtrips_celsius(client) -> None:
    """°C input converts at the write boundary; storage stays °F (#123)."""
    client.app["scheduler"]._active_unit = "C"
    try:
        resp = await client.put(
            f"/api/thermostats/{THERMO}",
            json={
                "eco_cooling_outdoor_threshold": 30,  # → 86.0 °F
                "eco_cooling_full_drift_temp": 38,  # → 100.4 °F
                "eco_cooling_max_drift": 2,  # Δ → 3.6 °F
                "eco_hysteresis_band": 1,  # Δ → 1.8 °F
            },
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["eco_cooling_outdoor_threshold"] == 86.0
        assert body["eco_cooling_full_drift_temp"] == 100.4
        assert body["eco_cooling_max_drift"] == 3.6
        assert body["eco_hysteresis_band"] == 1.8
    finally:
        client.app["scheduler"]._active_unit = "F"


@pytest.mark.asyncio
async def test_room_eco_override_nullable_inherit(client, fake_ha) -> None:
    await _configure_outdoor(client, fake_ha, 80.0)  # required to enable Eco on
    resp = await client.post(
        "/api/rooms",
        json={
            "name": "Bedroom",
            "thermostat_entity_id": THERMO,
            "eco_mode_enabled": True,
            "eco_cooling_max_drift": 6,
        },
    )
    assert resp.status == 201
    room = await resp.json()
    assert room["eco_mode_enabled"] is True
    assert room["eco_cooling_max_drift"] == 6.0
    # Unset fields inherit (stored NULL).
    assert room["eco_cooling_outdoor_threshold"] is None
    assert room["eco_hysteresis_band"] is None

    # Clearing a field with null restores inheritance.
    resp = await client.put(
        f"/api/rooms/{room['id']}", json={"eco_cooling_max_drift": None, "eco_mode_enabled": None}
    )
    body = await resp.json()
    assert body["eco_cooling_max_drift"] is None
    assert body["eco_mode_enabled"] is None


@pytest.mark.asyncio
async def test_eco_config_rejects_out_of_range(client) -> None:
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"eco_cooling_max_drift": 999})
    assert resp.status == 400
    resp = await client.put(
        f"/api/thermostats/{THERMO}", json={"eco_cooling_outdoor_threshold": 500}
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_thermostat_eco_field_rejects_null(client) -> None:
    """Thermostat Eco fields are non-null; null is rejected (rooms allow it)."""
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"eco_cooling_max_drift": None})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_create_thermostat_with_eco_fields(client, fake_ha) -> None:
    """POST /api/thermostats persists Eco config on the create path too."""
    await _configure_outdoor(client, fake_ha, 80.0)  # required to enable Eco
    resp = await client.post(
        "/api/thermostats",
        json={
            "thermostat_entity_id": "climate.new_eco",
            "name": "New",
            "total_vents_count": 4,
            "eco_mode_enabled": True,
            "eco_cooling_outdoor_threshold": 88,
            "eco_heating_max_drift": 5,
        },
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["eco_mode_enabled"] is True
    assert body["eco_cooling_outdoor_threshold"] == 88.0
    assert body["eco_heating_max_drift"] == 5.0


@pytest.mark.asyncio
async def test_create_thermostat_rejects_bad_eco(client) -> None:
    resp = await client.post(
        "/api/thermostats",
        json={
            "thermostat_entity_id": "climate.bad_eco",
            "total_vents_count": 4,
            "eco_cooling_max_drift": 999,
        },
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_create_room_rejects_invalid_eco(client) -> None:
    resp = await client.post(
        "/api/rooms",
        json={
            "name": "Bad",
            "thermostat_entity_id": THERMO,
            "eco_cooling_max_drift": 999,  # out of range
        },
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_update_room_rejects_non_numeric_eco(client) -> None:
    resp = await client.post("/api/rooms", json={"name": "R", "thermostat_entity_id": THERMO})
    room_id = (await resp.json())["id"]
    # A non-numeric (string) value is rejected on the nullable room path too.
    resp = await client.put(f"/api/rooms/{room_id}", json={"eco_cooling_max_drift": "hot"})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_update_room_returns_on_validation_error(client) -> None:
    """update_room short-circuits on the first invalid field — the error-return
    path the Eco validation block shares in this handler."""
    resp = await client.post("/api/rooms", json={"name": "R", "thermostat_entity_id": THERMO})
    room_id = (await resp.json())["id"]
    resp = await client.put(f"/api/rooms/{room_id}", json={"ambient_suppression_mode": "bogus"})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_thermostat_eco_enable_requires_outside_sensor(client) -> None:
    """Eco cannot be enabled on a thermostat without an outside-temp sensor
    (Issue #404 comment). The error nudges toward a weather integration. Both the
    update (PUT) and create (POST) paths enforce it."""
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"eco_mode_enabled": True})
    assert resp.status == 400
    body = await resp.json()
    assert "outside-temperature sensor" in body["error"]
    assert "PirateWeather" in body["error"]

    resp = await client.post(
        "/api/thermostats",
        json={
            "thermostat_entity_id": "climate.new",
            "total_vents_count": 4,
            "eco_mode_enabled": True,
        },
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_thermostat_eco_config_allowed_without_sensor(client) -> None:
    """Tuning the numeric Eco config (without enabling) needs no sensor — only
    turning it on does."""
    resp = await client.put(
        f"/api/thermostats/{THERMO}", json={"eco_cooling_outdoor_threshold": 88}
    )
    assert resp.status == 200
    assert (await resp.json())["eco_cooling_outdoor_threshold"] == 88.0


@pytest.mark.asyncio
async def test_room_eco_enable_requires_outside_sensor(client) -> None:
    """Forcing Eco on for a room also requires a configured outside-temp sensor."""
    resp = await client.post(
        "/api/rooms",
        json={"name": "R", "thermostat_entity_id": THERMO, "eco_mode_enabled": True},
    )
    assert resp.status == 400
    # A room that inherits (null) or opts out (false) needs no sensor.
    resp = await client.post(
        "/api/rooms",
        json={"name": "R2", "thermostat_entity_id": THERMO, "eco_mode_enabled": False},
    )
    assert resp.status == 201
    room_id = (await resp.json())["id"]
    # Updating that room to force Eco on is also blocked without a sensor.
    resp = await client.put(f"/api/rooms/{room_id}", json={"eco_mode_enabled": True})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_eco_impact_metric(client, fake_ha, tick) -> None:
    """Drive an Eco cycle to completion, then query the eco_impact metric."""
    _seed_warm_room(fake_ha, room_temp=78.0)
    await _configure_outdoor(client, fake_ha, 95.0)
    await _create_cooling_room(client, target_temp=70.0)
    await client.put(f"/api/thermostats/{THERMO}", json=_STEP_ECO)

    await tick()  # cycle starts, target relaxed to 74
    # Room now reaches the relaxed target → cycle can complete.
    fake_ha.seed_state("sensor.test_room_temp", "73.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 73.0, "temperature": 72.0, "hvac_action": "idle"},
    )
    await tick()
    await tick()

    impact = await (await client.get(f"/api/metrics/thermostats/{THERMO}/eco-impact")).json()
    assert impact["eco_active_cycles"] >= 1
    assert impact["avg_drift_f"] == pytest.approx(4.0, abs=0.5)
    assert impact["rooms"] and impact["rooms"][0]["eco_active_cycles"] >= 1
    assert impact["rooms"][0]["avg_drift_f"] == pytest.approx(4.0, abs=0.5)

    # Home-wide variant has the same shape.
    home = await (await client.get("/api/metrics/thermostats/eco-impact")).json()
    assert home["thermostat_entity_id"] is None
    assert home["eco_active_cycles"] >= 1


@pytest.mark.asyncio
async def test_eco_relaxed_target_clamped_to_max_setpoint(client, fake_ha, tick) -> None:
    """Eco relaxes a cooling target *upward* (warmer). Even so, the relaxed
    effective target must respect the thermostat's ``max_setpoint`` safety
    ceiling — Eco can never warm a room past the configured upper bound. Here the
    step config would relax 70 → 74, but a 71°F ceiling clamps it to 71.
    """
    _seed_warm_room(fake_ha)
    await _configure_outdoor(client, fake_ha, 95.0)
    await _create_cooling_room(client, target_temp=70.0)
    await client.put(
        f"/api/thermostats/{THERMO}",
        json={**_STEP_ECO, "min_setpoint": 55, "max_setpoint": 71},
    )

    await tick()

    logs = await (await client.get("/api/logs")).json()
    detail = await (await client.get(f"/api/logs/{logs[0]['id']}/detail")).json()
    room = detail["rooms"][0]
    assert room["requested_target"] == pytest.approx(70.0)
    # The would-be 74°F relaxation is capped at the 71°F ceiling, not applied raw.
    assert room["effective_target"] == pytest.approx(71.0)
    assert room["effective_target"] <= 71.0
    assert room["eco_active"] is True  # relaxation still happened, just clamped

    for call in fake_ha.calls_for("set_temperature"):
        assert call.data["temperature"] <= 71.0, (
            f"eco setpoint {call.data['temperature']} breached max_setpoint=71"
        )


@pytest.mark.asyncio
async def test_eco_relaxed_target_clamped_to_min_setpoint(client, fake_ha, tick) -> None:
    """Symmetric lower bound: Eco relaxes a heating target *downward* (cooler),
    but never below ``min_setpoint``. The step config would relax 70 → 66, yet a
    68°F floor clamps it to 68.
    """
    # Cold outside + heat mode + cold room → an engaged heating relaxation.
    fake_ha.seed_state(
        THERMO,
        "heat",
        {"current_temperature": 60.0, "temperature": 62.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state("sensor.test_room_temp", "60.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.test_room_vent", "open", {})
    await _configure_outdoor(client, fake_ha, 0.0)  # well below the heating threshold
    await _create_cooling_room(client, target_temp=70.0)  # room+schedule; heat comes from seed
    await client.put(
        f"/api/thermostats/{THERMO}",
        json={
            "eco_mode_enabled": True,
            "eco_heating_outdoor_threshold": 40,
            "eco_heating_full_drift_temp": 40,  # step: full 4°F drift once engaged
            "eco_heating_max_drift": 4,
            "min_setpoint": 68,
            "max_setpoint": 85,
        },
    )

    await tick()

    logs = await (await client.get("/api/logs")).json()
    detail = await (await client.get(f"/api/logs/{logs[0]['id']}/detail")).json()
    room = detail["rooms"][0]
    assert room["requested_target"] == pytest.approx(70.0)
    # The would-be 66°F relaxation is raised to the 68°F floor, not applied raw.
    assert room["effective_target"] == pytest.approx(68.0)
    assert room["effective_target"] >= 68.0
    assert room["eco_active"] is True

    for call in fake_ha.calls_for("set_temperature"):
        assert call.data["temperature"] >= 68.0, (
            f"eco setpoint {call.data['temperature']} breached min_setpoint=68"
        )
