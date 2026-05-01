"""
Tests for Issue #123 Phase 2 — input conversion & precision.

Covers:
- _to_f() helper (absolute temperature conversion)
- _delta_to_f() helper (delta/offset conversion)
- All route handlers that accept temperature values:
  POST/PUT /api/rooms           (system_wide_temp, temp_offset)
  POST/PUT /api/rooms/.../schedules  (target_temp)
  POST/PUT /api/thermostats         (default_temp, min/max_setpoint, deadband, overshoot_delta)
  POST     /api/rooms/.../override  (target_temp)
"""

from __future__ import annotations

import contextlib
import os
import tempfile

import pytest
from aiohttp.test_utils import TestClient, TestServer

from backend.api.routes import _delta_to_f, _to_f
from backend.main import build_app

from .integration.fake_ha import FakeHomeAssistant

# ---------------------------------------------------------------------------
# Conversion helper unit tests
# ---------------------------------------------------------------------------


class TestToF:
    def test_fahrenheit_input_is_noop(self):
        assert _to_f(70.0, "F") == 70.0
        assert _to_f(68.5, "F") == 68.5

    def test_fahrenheit_rounds_to_2dp(self):
        assert _to_f(70.123456, "F") == 70.12

    def test_celsius_converts_freezing(self):
        assert _to_f(0.0, "C") == 32.0

    def test_celsius_converts_room_temp(self):
        assert _to_f(20.0, "C") == 68.0

    def test_celsius_precision_2dp(self):
        # 6.3°C → 43.34°F (not 43.3 — the 2dp fix)
        assert _to_f(6.3, "C") == 43.34

    def test_celsius_21_degrees(self):
        # 21°C → 69.8°F
        assert _to_f(21.0, "C") == 69.8


class TestDeltaToF:
    def test_fahrenheit_input_is_noop(self):
        assert _delta_to_f(0.5, "F") == 0.5

    def test_fahrenheit_rounds_to_2dp(self):
        assert _delta_to_f(1.234567, "F") == 1.23

    def test_celsius_delta_no_offset(self):
        # 0.3°C delta → 0.54°F (multiply only, no +32)
        assert _delta_to_f(0.3, "C") == 0.54

    def test_celsius_delta_2_degrees(self):
        # 2.0°C → 3.6°F
        assert _delta_to_f(2.0, "C") == 3.6

    def test_celsius_delta_does_not_add_offset(self):
        # Ensure +32 is NOT applied — 1°C delta must be 1.8°F, not 33.8°F
        assert _delta_to_f(1.0, "C") == 1.8


# ---------------------------------------------------------------------------
# Fixture — test client with Celsius unit active
# ---------------------------------------------------------------------------


@pytest.fixture
async def celsius_client():
    """A running app where the scheduler reports active unit = 'C'."""
    fake_ha = FakeHomeAssistant()
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        app = build_app(fake_ha, db_path, frontend_dist=None, start_ha=False)
        server = TestServer(app)
        async with TestClient(server) as c:
            await c.start_server()
            c.app["scheduler"]._active_unit = "C"
            yield c
    finally:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.unlink(db_path + suffix)


@pytest.fixture
async def fahrenheit_client():
    """A running app where the scheduler reports active unit = 'F' (default)."""
    fake_ha = FakeHomeAssistant()
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        app = build_app(fake_ha, db_path, frontend_dist=None, start_ha=False)
        server = TestServer(app)
        async with TestClient(server) as c:
            await c.start_server()
            yield c
    finally:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.unlink(db_path + suffix)


# ---------------------------------------------------------------------------
# POST /api/rooms — system_wide_temp conversion
# ---------------------------------------------------------------------------


class TestCreateRoomConversion:
    async def test_system_wide_temp_converted_from_celsius(self, celsius_client):
        resp = await celsius_client.post(
            "/api/rooms",
            json={
                "name": "Bedroom",
                "thermostat_entity_id": "climate.test",
                "system_wide_temp": 20.0,
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["system_wide_temp"] == 68.0  # 20°C → 68°F

    async def test_system_wide_temp_no_conversion_in_fahrenheit(self, fahrenheit_client):
        resp = await fahrenheit_client.post(
            "/api/rooms",
            json={
                "name": "Bedroom",
                "thermostat_entity_id": "climate.test",
                "system_wide_temp": 70.0,
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["system_wide_temp"] == 70.0

    async def test_system_wide_temp_none_stays_none(self, celsius_client):
        resp = await celsius_client.post(
            "/api/rooms",
            json={"name": "Bedroom", "thermostat_entity_id": "climate.test"},
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["system_wide_temp"] is None


# ---------------------------------------------------------------------------
# PUT /api/rooms/{room_id} — system_wide_temp and temp_offset conversion
# ---------------------------------------------------------------------------


class TestUpdateRoomConversion:
    async def _make_room(self, client):
        resp = await client.post(
            "/api/rooms",
            json={"name": "Living Room", "thermostat_entity_id": "climate.test"},
        )
        assert resp.status == 201
        return (await resp.json())["id"]

    async def test_system_wide_temp_converted(self, celsius_client):
        room_id = await self._make_room(celsius_client)
        resp = await celsius_client.put(
            f"/api/rooms/{room_id}",
            json={"system_wide_temp": 21.0},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["system_wide_temp"] == 69.8  # 21°C → 69.8°F

    async def test_temp_offset_delta_converted(self, celsius_client):
        room_id = await self._make_room(celsius_client)
        resp = await celsius_client.put(
            f"/api/rooms/{room_id}",
            json={"temp_offset": 1.0},  # 1°C delta → 1.8°F
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["temp_offset"] == 1.8

    async def test_system_wide_temp_none_clears(self, celsius_client):
        room_id = await self._make_room(celsius_client)
        resp = await celsius_client.put(
            f"/api/rooms/{room_id}",
            json={"system_wide_temp": None},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["system_wide_temp"] is None

    async def test_no_conversion_in_fahrenheit(self, fahrenheit_client):
        room_id = await self._make_room(fahrenheit_client)
        resp = await fahrenheit_client.put(
            f"/api/rooms/{room_id}",
            json={"temp_offset": 2.0},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["temp_offset"] == 2.0


# ---------------------------------------------------------------------------
# POST /api/rooms/{room_id}/schedules — target_temp conversion
# ---------------------------------------------------------------------------


class TestCreateScheduleConversion:
    async def _make_room(self, client):
        resp = await client.post(
            "/api/rooms",
            json={"name": "Office", "thermostat_entity_id": "climate.test"},
        )
        assert resp.status == 201
        return (await resp.json())["id"]

    async def test_target_temp_converted_from_celsius(self, celsius_client):
        room_id = await self._make_room(celsius_client)
        resp = await celsius_client.post(
            f"/api/rooms/{room_id}/schedules",
            json={
                "days_of_week": [0, 1, 2, 3, 4],
                "start_time": "08:00",
                "end_time": "18:00",
                "target_temp": 20.0,
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["target_temp"] == 68.0

    async def test_target_temp_precision_celsius(self, celsius_client):
        room_id = await self._make_room(celsius_client)
        resp = await celsius_client.post(
            f"/api/rooms/{room_id}/schedules",
            json={
                "days_of_week": [5, 6],
                "start_time": "10:00",
                "end_time": "20:00",
                "target_temp": 6.3,  # → 43.34°F (not 43.3)
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["target_temp"] == 43.34

    async def test_target_temp_not_converted_in_fahrenheit(self, fahrenheit_client):
        room_id = await self._make_room(fahrenheit_client)
        resp = await fahrenheit_client.post(
            f"/api/rooms/{room_id}/schedules",
            json={
                "days_of_week": [0],
                "start_time": "09:00",
                "end_time": "17:00",
                "target_temp": 72.0,
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["target_temp"] == 72.0


# ---------------------------------------------------------------------------
# PUT /api/rooms/{room_id}/schedules/{schedule_id} — target_temp conversion
# ---------------------------------------------------------------------------


class TestUpdateScheduleConversion:
    async def _make_room_and_schedule(self, client):
        resp = await client.post(
            "/api/rooms",
            json={"name": "Den", "thermostat_entity_id": "climate.test"},
        )
        room_id = (await resp.json())["id"]
        resp2 = await client.post(
            f"/api/rooms/{room_id}/schedules",
            json={
                "days_of_week": [0],
                "start_time": "08:00",
                "end_time": "18:00",
                "target_temp": 70.0,
            },
        )
        schedule_id = (await resp2.json())["id"]
        return room_id, schedule_id

    async def test_target_temp_converted_on_update(self, celsius_client):
        room_id, sched_id = await self._make_room_and_schedule(celsius_client)
        resp = await celsius_client.put(
            f"/api/rooms/{room_id}/schedules/{sched_id}",
            json={"target_temp": 22.0},  # 22°C → 71.6°F
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["target_temp"] == 71.6


# ---------------------------------------------------------------------------
# POST /api/thermostats — temperature field conversion
# ---------------------------------------------------------------------------


class TestCreateThermostatConversion:
    async def test_absolute_fields_converted(self, celsius_client):
        resp = await celsius_client.post(
            "/api/thermostats",
            json={
                "thermostat_entity_id": "climate.main",
                "default_temp": 20.0,  # → 68.0°F
                "min_setpoint": 16.0,  # → 60.8°F
                "max_setpoint": 26.0,  # → 78.8°F
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["default_temp"] == 68.0
        assert data["min_setpoint"] == 60.8
        assert data["max_setpoint"] == 78.8

    async def test_delta_fields_converted(self, celsius_client):
        resp = await celsius_client.post(
            "/api/thermostats",
            json={
                "thermostat_entity_id": "climate.main2",
                "deadband": 0.5,  # 0.5°C → 0.9°F
                "overshoot_delta": 1.0,  # 1°C → 1.8°F
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["deadband"] == 0.9
        assert data["overshoot_delta"] == 1.8

    async def test_non_temp_fields_not_converted(self, celsius_client):
        resp = await celsius_client.post(
            "/api/thermostats",
            json={
                "thermostat_entity_id": "climate.main3",
                "max_vent_closed_min": 15,
                "min_open_vents": 2,
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["max_vent_closed_min"] == 15
        assert data["min_open_vents"] == 2

    async def test_no_conversion_in_fahrenheit(self, fahrenheit_client):
        resp = await fahrenheit_client.post(
            "/api/thermostats",
            json={
                "thermostat_entity_id": "climate.main4",
                "default_temp": 70.0,
                "deadband": 0.5,
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["default_temp"] == 70.0
        assert data["deadband"] == 0.5


# ---------------------------------------------------------------------------
# PUT /api/thermostats/{entity_id} — temperature field conversion
# ---------------------------------------------------------------------------


class TestUpsertThermostatConversion:
    async def test_absolute_and_delta_fields_converted(self, celsius_client):
        resp = await celsius_client.put(
            "/api/thermostats/climate.living",
            json={
                "min_setpoint": 18.0,  # → 64.4°F
                "overshoot_delta": 2.0,  # → 3.6°F
            },
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["min_setpoint"] == 64.4
        assert data["overshoot_delta"] == 3.6


# ---------------------------------------------------------------------------
# POST /api/rooms/{room_id}/override — target_temp conversion
# ---------------------------------------------------------------------------


class TestSetOverrideConversion:
    async def test_override_target_temp_converted(self, celsius_client):
        resp = await celsius_client.post(
            "/api/rooms",
            json={"name": "Sunroom", "thermostat_entity_id": "climate.test"},
        )
        room_id = (await resp.json())["id"]

        resp2 = await celsius_client.post(
            f"/api/rooms/{room_id}/override",
            json={"target_temp": 22.0, "duration_hours": 1},
        )
        assert resp2.status == 200
        data = await resp2.json()
        assert data["target_temp"] == 71.6  # 22°C → 71.6°F

    async def test_override_not_converted_in_fahrenheit(self, fahrenheit_client):
        resp = await fahrenheit_client.post(
            "/api/rooms",
            json={"name": "Sunroom", "thermostat_entity_id": "climate.test"},
        )
        room_id = (await resp.json())["id"]

        resp2 = await fahrenheit_client.post(
            f"/api/rooms/{room_id}/override",
            json={"target_temp": 72.0, "duration_hours": 1},
        )
        assert resp2.status == 200
        data = await resp2.json()
        assert data["target_temp"] == 72.0
