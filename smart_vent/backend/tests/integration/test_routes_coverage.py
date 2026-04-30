"""
Additional integration tests to raise coverage on api/routes.py.

The existing integration tests focus on cycle flow and metrics. This file
covers the many CRUD endpoints, validation branches, and utility handlers
that are currently uncovered.
"""

from __future__ import annotations

import io
import os
import tempfile

import pytest
from aiohttp.test_utils import TestClient, TestServer

from backend.main import build_app

from .fake_ha import FakeHomeAssistant


# ---------------------------------------------------------------------------
# Shared helpers (inline — avoid import cost)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_ha():
    return FakeHomeAssistant()


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        yield path
    finally:
        for suffix in ("", "-wal", "-shm"):
            p = path + suffix
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


@pytest.fixture
async def client(fake_ha, db_path):
    app = build_app(fake_ha, db_path, frontend_dist=None, start_ha=False)
    server = TestServer(app)
    async with TestClient(server) as c:
        await c.start_server()
        yield c


# ---------------------------------------------------------------------------
# Helper to create a room via API
# ---------------------------------------------------------------------------


async def _create_room(client, name="Living Room", thermostat="climate.test"):
    resp = await client.post(
        "/api/rooms",
        json={"name": name, "thermostat_entity_id": thermostat},
    )
    assert resp.status == 201
    return await resp.json()


# ---------------------------------------------------------------------------
# Rooms — validation & update & delete
# ---------------------------------------------------------------------------


class TestRoomsValidation:
    async def test_create_room_missing_name(self, client):
        resp = await client.post("/api/rooms", json={"thermostat_entity_id": "climate.x"})
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_create_room_missing_thermostat(self, client):
        resp = await client.post("/api/rooms", json={"name": "Room"})
        assert resp.status == 400

    async def test_get_room_not_found(self, client):
        resp = await client.get("/api/rooms/nonexistent-id")
        assert resp.status == 404

    async def test_update_room(self, client):
        room = await _create_room(client)
        resp = await client.put(
            f"/api/rooms/{room['id']}",
            json={"name": "Updated Room", "notes": "new note"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["name"] == "Updated Room"

    async def test_update_room_not_found(self, client):
        resp = await client.put("/api/rooms/bad-id", json={"name": "x"})
        assert resp.status == 404

    async def test_delete_room(self, client):
        room = await _create_room(client)
        resp = await client.delete(f"/api/rooms/{room['id']}")
        assert resp.status == 200
        data = await resp.json()
        assert "deleted" in data

    async def test_delete_room_not_found(self, client):
        resp = await client.delete("/api/rooms/no-such-id")
        assert resp.status == 404


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------


class TestSensors:
    async def test_list_sensors(self, client):
        room = await _create_room(client)
        resp = await client.get(f"/api/rooms/{room['id']}/sensors")
        assert resp.status == 200
        assert await resp.json() == []

    async def test_add_sensor_missing_entity_id(self, client):
        room = await _create_room(client)
        resp = await client.post(f"/api/rooms/{room['id']}/sensors", json={})
        assert resp.status == 400

    async def test_add_and_remove_sensor(self, client):
        room = await _create_room(client)
        resp = await client.post(
            f"/api/rooms/{room['id']}/sensors",
            json={"entity_id": "sensor.bedroom_temp"},
        )
        assert resp.status == 201
        resp = await client.delete(
            f"/api/rooms/{room['id']}/sensors/sensor.bedroom_temp"
        )
        assert resp.status == 200


# ---------------------------------------------------------------------------
# Vents
# ---------------------------------------------------------------------------


class TestVents:
    async def test_list_vents(self, client):
        room = await _create_room(client)
        resp = await client.get(f"/api/rooms/{room['id']}/vents")
        assert resp.status == 200

    async def test_add_vent_missing_entity_id(self, client):
        room = await _create_room(client)
        resp = await client.post(f"/api/rooms/{room['id']}/vents", json={})
        assert resp.status == 400

    async def test_add_vent_invalid_control_method(self, client):
        room = await _create_room(client)
        resp = await client.post(
            f"/api/rooms/{room['id']}/vents",
            json={"entity_id": "cover.v1", "control_method": "not_valid"},
        )
        assert resp.status == 400

    async def test_add_vent_and_update_and_remove(self, client):
        room = await _create_room(client)
        resp = await client.post(
            f"/api/rooms/{room['id']}/vents",
            json={"entity_id": "cover.v1", "control_method": "open_close"},
        )
        assert resp.status == 201

        resp = await client.patch(
            f"/api/rooms/{room['id']}/vents/cover.v1",
            json={"control_method": "set_position"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["control_method"] == "set_position"

        resp = await client.delete(f"/api/rooms/{room['id']}/vents/cover.v1")
        assert resp.status == 200

    async def test_update_vent_missing_control_method(self, client):
        room = await _create_room(client)
        resp = await client.patch(f"/api/rooms/{room['id']}/vents/cover.v1", json={})
        assert resp.status == 400

    async def test_update_vent_invalid_control_method(self, client):
        room = await _create_room(client)
        resp = await client.patch(
            f"/api/rooms/{room['id']}/vents/cover.v1",
            json={"control_method": "nope"},
        )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# Vent test endpoint
# ---------------------------------------------------------------------------


class TestVentTest:
    async def test_vent_test_missing_entity_id(self, client):
        resp = await client.post(
            "/api/vents/test",
            json={"control_method": "open_close", "direction": "open"},
        )
        assert resp.status == 400

    async def test_vent_test_invalid_method(self, client):
        resp = await client.post(
            "/api/vents/test",
            json={"entity_id": "cover.v1", "control_method": "bad", "direction": "open"},
        )
        assert resp.status == 400

    async def test_vent_test_invalid_direction(self, client):
        resp = await client.post(
            "/api/vents/test",
            json={"entity_id": "cover.v1", "control_method": "open_close", "direction": "sideways"},
        )
        assert resp.status == 400

    async def test_vent_test_open_close(self, client, fake_ha):
        fake_ha.seed_state("cover.v1", "closed", {})
        resp = await client.post(
            "/api/vents/test",
            json={"entity_id": "cover.v1", "control_method": "open_close", "direction": "open"},
        )
        assert resp.status == 200

    async def test_vent_test_close_direction(self, client, fake_ha):
        fake_ha.seed_state("cover.v1", "open", {})
        resp = await client.post(
            "/api/vents/test",
            json={"entity_id": "cover.v1", "control_method": "open_close", "direction": "close"},
        )
        assert resp.status == 200

    async def test_vent_test_set_position_open(self, client, fake_ha):
        fake_ha.seed_state("cover.v1", "closed", {})
        resp = await client.post(
            "/api/vents/test",
            json={"entity_id": "cover.v1", "control_method": "set_position", "direction": "open"},
        )
        assert resp.status == 200

    async def test_vent_test_set_position_close(self, client, fake_ha):
        fake_ha.seed_state("cover.v1", "open", {})
        resp = await client.post(
            "/api/vents/test",
            json={"entity_id": "cover.v1", "control_method": "set_position", "direction": "close"},
        )
        assert resp.status == 200

    async def test_vent_test_set_tilt_open(self, client, fake_ha):
        fake_ha.seed_state("cover.v1", "closed", {})
        resp = await client.post(
            "/api/vents/test",
            json={"entity_id": "cover.v1", "control_method": "set_tilt_position", "direction": "open"},
        )
        assert resp.status == 200

    async def test_vent_test_set_tilt_close(self, client, fake_ha):
        fake_ha.seed_state("cover.v1", "open", {})
        resp = await client.post(
            "/api/vents/test",
            json={"entity_id": "cover.v1", "control_method": "set_tilt_position", "direction": "close"},
        )
        assert resp.status == 200

    async def test_vent_test_toggle_open(self, client, fake_ha):
        fake_ha.seed_state("cover.v1", "closed", {})
        resp = await client.post(
            "/api/vents/test",
            json={"entity_id": "cover.v1", "control_method": "toggle", "direction": "open"},
        )
        assert resp.status == 200

    async def test_vent_test_toggle_close(self, client, fake_ha):
        fake_ha.seed_state("cover.v1", "open", {})
        resp = await client.post(
            "/api/vents/test",
            json={"entity_id": "cover.v1", "control_method": "toggle", "direction": "close"},
        )
        assert resp.status == 200


# ---------------------------------------------------------------------------
# Presence sensors
# ---------------------------------------------------------------------------


class TestPresenceSensors:
    async def test_list_presence(self, client):
        room = await _create_room(client)
        resp = await client.get(f"/api/rooms/{room['id']}/presence")
        assert resp.status == 200

    async def test_add_presence_missing_entity_id(self, client):
        room = await _create_room(client)
        resp = await client.post(f"/api/rooms/{room['id']}/presence", json={})
        assert resp.status == 400

    async def test_add_and_remove_presence(self, client):
        room = await _create_room(client)
        resp = await client.post(
            f"/api/rooms/{room['id']}/presence",
            json={"entity_id": "binary_sensor.presence"},
        )
        assert resp.status == 201
        resp = await client.delete(
            f"/api/rooms/{room['id']}/presence/binary_sensor.presence"
        )
        assert resp.status == 200


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


class TestSchedules:
    async def test_list_schedules(self, client):
        room = await _create_room(client)
        resp = await client.get(f"/api/rooms/{room['id']}/schedules")
        assert resp.status == 200
        assert await resp.json() == []

    async def test_create_schedule_missing_fields(self, client):
        room = await _create_room(client)
        resp = await client.post(
            f"/api/rooms/{room['id']}/schedules",
            json={"days_of_week": [0]},
        )
        assert resp.status == 400

    async def test_create_schedule_bad_time(self, client):
        room = await _create_room(client)
        resp = await client.post(
            f"/api/rooms/{room['id']}/schedules",
            json={
                "days_of_week": [0],
                "start_time": "not-a-time",
                "end_time": "09:00",
                "target_temp": 70.0,
            },
        )
        assert resp.status == 400

    async def test_create_and_update_and_delete_schedule(self, client):
        room = await _create_room(client)
        resp = await client.post(
            f"/api/rooms/{room['id']}/schedules",
            json={
                "days_of_week": [0, 1],
                "start_time": "08:00",
                "end_time": "18:00",
                "target_temp": 72.0,
            },
        )
        assert resp.status == 201
        sched = await resp.json()

        resp = await client.put(
            f"/api/rooms/{room['id']}/schedules/{sched['id']}",
            json={"target_temp": 74.0},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["target_temp"] == 74.0

        resp = await client.delete(
            f"/api/rooms/{room['id']}/schedules/{sched['id']}"
        )
        assert resp.status == 200

    async def test_update_schedule_not_found(self, client):
        room = await _create_room(client)
        resp = await client.put(
            f"/api/rooms/{room['id']}/schedules/no-such-id",
            json={"target_temp": 70.0},
        )
        assert resp.status == 404

    async def test_create_schedule_overlap_rejected(self, client):
        room = await _create_room(client)
        payload = {
            "days_of_week": [0],
            "start_time": "08:00",
            "end_time": "18:00",
            "target_temp": 70.0,
        }
        resp = await client.post(f"/api/rooms/{room['id']}/schedules", json=payload)
        assert resp.status == 201
        # Same days/times → overlap
        resp = await client.post(f"/api/rooms/{room['id']}/schedules", json=payload)
        assert resp.status == 400


# ---------------------------------------------------------------------------
# Thermostats
# ---------------------------------------------------------------------------


class TestThermostats:
    async def test_list_thermostats(self, client):
        resp = await client.get("/api/thermostats")
        assert resp.status == 200

    async def test_create_thermostat_missing_entity_id(self, client):
        resp = await client.post("/api/thermostats", json={"name": "Upstairs"})
        assert resp.status == 400

    async def test_create_thermostat(self, client):
        resp = await client.post(
            "/api/thermostats",
            json={"thermostat_entity_id": "climate.upstairs", "name": "Upstairs"},
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["thermostat_entity_id"] == "climate.upstairs"

    async def test_delete_thermostat(self, client):
        await client.post(
            "/api/thermostats",
            json={"thermostat_entity_id": "climate.main"},
        )
        resp = await client.delete("/api/thermostats/climate.main")
        assert resp.status == 200
        data = await resp.json()
        assert data["deleted"] == "climate.main"


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


class TestOverrides:
    async def test_set_override(self, client):
        room = await _create_room(client)
        resp = await client.post(
            f"/api/rooms/{room['id']}/override",
            json={"target_temp": 75.0, "duration_hours": 1.0},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["target_temp"] == 75.0

    async def test_set_override_missing_target_temp(self, client):
        room = await _create_room(client)
        resp = await client.post(
            f"/api/rooms/{room['id']}/override",
            json={"duration_hours": 1.0},
        )
        assert resp.status == 400

    async def test_clear_override(self, client):
        room = await _create_room(client)
        resp = await client.delete(f"/api/rooms/{room['id']}/override")
        assert resp.status == 200


# ---------------------------------------------------------------------------
# Active status
# ---------------------------------------------------------------------------


class TestRoomsActiveStatus:
    async def test_active_status_empty(self, client):
        resp = await client.post("/api/rooms/active-status", json={"room_ids": []})
        assert resp.status == 200
        assert await resp.json() == {}

    async def test_active_status_unknown_room_skipped(self, client):
        resp = await client.post(
            "/api/rooms/active-status", json={"room_ids": ["nonexistent"]}
        )
        assert resp.status == 200
        assert await resp.json() == {}

    async def test_active_status_known_room(self, client):
        room = await _create_room(client)
        resp = await client.post(
            "/api/rooms/active-status", json={"room_ids": [room["id"]]}
        )
        assert resp.status == 200
        data = await resp.json()
        assert room["id"] in data


# ---------------------------------------------------------------------------
# HA proxy endpoints
# ---------------------------------------------------------------------------


class TestHaProxy:
    async def test_ha_states_entity_not_in_cache(self, client):
        resp = await client.post(
            "/api/ha/states", json={"entity_ids": ["sensor.missing"]}
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["sensor.missing"] is None

    async def test_ha_states_numeric_entity(self, client, fake_ha):
        fake_ha.seed_state("sensor.temp", "72.5", {"unit_of_measurement": "°F"})
        resp = await client.post(
            "/api/ha/states", json={"entity_ids": ["sensor.temp"]}
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["sensor.temp"]["numeric"] == 72.5

    async def test_ha_states_celsius_converted(self, client, fake_ha):
        fake_ha.seed_state("sensor.c", "22.0", {"unit_of_measurement": "°C"})
        resp = await client.post("/api/ha/states", json={"entity_ids": ["sensor.c"]})
        assert resp.status == 200
        data = await resp.json()
        assert data["sensor.c"]["unit"] == "°F"

    async def test_ha_states_non_numeric_entity(self, client, fake_ha):
        fake_ha.seed_state("binary_sensor.door", "on", {})
        resp = await client.post(
            "/api/ha/states", json={"entity_ids": ["binary_sensor.door"]}
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["binary_sensor.door"]["numeric"] is None

    async def test_ha_entities_with_sensor_domain(self, client, fake_ha):
        fake_ha.seed_state("sensor.temp", "70", {})
        resp = await client.get("/api/ha/entities?domain=sensor")
        assert resp.status == 200
        data = await resp.json()
        assert any(e["entity_id"] == "sensor.temp" for e in data)

    async def test_ha_entities_with_domain(self, client, fake_ha):
        fake_ha.seed_state("climate.main", "cool", {"hvac_action": "cooling"})
        fake_ha.seed_state("sensor.temp", "70", {})
        resp = await client.get("/api/ha/entities?domain=climate")
        assert resp.status == 200
        data = await resp.json()
        assert any(e["entity_id"] == "climate.main" for e in data)
        assert not any(e["entity_id"] == "sensor.temp" for e in data)

    async def test_ha_entities_has_attribute_filter(self, client, fake_ha):
        fake_ha.seed_state("climate.main", "cool", {"hvac_action": "cooling"})
        fake_ha.seed_state("climate.bare", "heat", {})
        resp = await client.get("/api/ha/entities?domain=climate&has_attribute=hvac_action")
        assert resp.status == 200
        data = await resp.json()
        assert any(e["entity_id"] == "climate.main" for e in data)
        assert not any(e["entity_id"] == "climate.bare" for e in data)

    async def test_ha_entities_exclude_icon_filter(self, client, fake_ha):
        fake_ha.seed_state("cover.door", "open", {"icon": "mdi:door-open"})
        fake_ha.seed_state("cover.vent", "open", {"icon": "mdi:air-filter"})
        resp = await client.get("/api/ha/entities?domain=cover&exclude_icon=mdi:door-open")
        assert resp.status == 200
        data = await resp.json()
        assert not any(e["entity_id"] == "cover.door" for e in data)


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


class TestLogs:
    async def test_get_logs_empty(self, client):
        resp = await client.get("/api/logs")
        assert resp.status == 200
        assert await resp.json() == []

    async def test_get_log_detail_not_found(self, client):
        resp = await client.get("/api/logs/no-such-cycle/detail")
        assert resp.status == 404

    async def test_get_log_temp_samples(self, client):
        resp = await client.get("/api/logs/any-id/temp-samples")
        assert resp.status == 200
        assert await resp.json() == []

    async def test_get_event_logs(self, client):
        resp = await client.get("/api/logs/events")
        assert resp.status == 200

    async def test_get_event_logs_with_filters(self, client):
        resp = await client.get("/api/logs/events?category=system&level=info&limit=5")
        assert resp.status == 200

    async def test_clear_event_logs(self, client):
        resp = await client.delete("/api/logs/events")
        assert resp.status == 200
        data = await resp.json()
        assert data["cleared"] is True


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    async def test_get_log_retention(self, client):
        resp = await client.get("/api/settings/log-retention")
        assert resp.status == 200
        data = await resp.json()
        assert "event_log_retention_days" in data
        assert "cycle_log_retention_days" in data

    async def test_set_log_retention(self, client):
        resp = await client.post(
            "/api/settings/log-retention",
            json={"event_log_retention_days": 14, "cycle_log_retention_days": 60},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["event_log_retention_days"] == 14
        assert data["cycle_log_retention_days"] == 60


# ---------------------------------------------------------------------------
# System endpoints
# ---------------------------------------------------------------------------


class TestSystemEndpoints:
    async def test_system_status(self, client):
        resp = await client.get("/api/system/status")
        assert resp.status == 200
        data = await resp.json()
        assert "enabled" in data
        assert "dev_mode" in data

    async def test_get_dev_mode(self, client):
        resp = await client.get("/api/system/dev-mode")
        assert resp.status == 200
        assert "dev_mode" in await resp.json()

    async def test_set_dev_mode(self, client):
        resp = await client.post("/api/system/dev-mode", json={"dev_mode": True})
        assert resp.status == 200
        data = await resp.json()
        assert data["dev_mode"] is True

    async def test_set_dev_mode_missing_field(self, client):
        resp = await client.post("/api/system/dev-mode", json={})
        assert resp.status == 400

    async def test_set_system_enabled_missing_field(self, client):
        resp = await client.post("/api/system/enabled", json={})
        assert resp.status == 400

    async def test_set_system_enabled(self, client):
        resp = await client.post("/api/system/enabled", json={"enabled": False})
        assert resp.status == 200
        data = await resp.json()
        assert data["enabled"] is False

    async def test_zone_status(self, client):
        resp = await client.get("/api/status")
        assert resp.status == 200


# ---------------------------------------------------------------------------
# Backup & Restore
# ---------------------------------------------------------------------------


class TestBackupRestore:
    async def test_backup_returns_file(self, client):
        resp = await client.get("/api/backup")
        assert resp.status == 200
        assert resp.content_type == "application/octet-stream"
        body = await resp.read()
        assert body[:16] == b"SQLite format 3\x00"

    async def test_restore_non_sqlite_file_rejected(self, client):
        data = io.BytesIO(b"not a sqlite database content here!!")
        form_data = {"file": data}
        resp = await client.post("/api/restore", data=form_data)
        assert resp.status == 400
        body = await resp.json()
        assert "error" in body

    async def test_restore_valid_db(self, client, db_path):
        # First take a backup, then restore it
        backup_resp = await client.get("/api/backup")
        assert backup_resp.status == 200
        db_bytes = await backup_resp.read()

        data = io.BytesIO(db_bytes)
        resp = await client.post("/api/restore", data={"file": data})
        assert resp.status == 200
        result = await resp.json()
        assert result["restored"] is True

    async def test_restore_missing_file_field(self, client):
        resp = await client.post("/api/restore", data={"wrong_field": b"data"})
        assert resp.status == 400
