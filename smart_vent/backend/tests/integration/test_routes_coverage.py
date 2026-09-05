"""
Additional integration tests to raise coverage on api/routes.py.

The existing integration tests focus on cycle flow and metrics. This file
covers the many CRUD endpoints, validation branches, and utility handlers
that are currently uncovered.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import signal
import tempfile
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from backend import db
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
            with contextlib.suppress(OSError):
                os.unlink(p)


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

    async def test_create_room_invalid_holdover_negative(self, client):
        resp = await client.post(
            "/api/rooms",
            json={
                "name": "Room",
                "thermostat_entity_id": "climate.x",
                "presence_holdover_hours": -1,
            },
        )
        assert resp.status == 400
        assert "presence_holdover_hours" in (await resp.json())["error"]

    async def test_create_room_invalid_holdover_non_numeric(self, client):
        resp = await client.post(
            "/api/rooms",
            json={
                "name": "Room",
                "thermostat_entity_id": "climate.x",
                "presence_holdover_hours": "bad",
            },
        )
        assert resp.status == 400

    async def test_create_room_invalid_temp_offset_non_numeric(self, client):
        resp = await client.post(
            "/api/rooms",
            json={"name": "Room", "thermostat_entity_id": "climate.x", "temp_offset": "bad"},
        )
        assert resp.status == 400

    async def test_create_room_invalid_temp_offset_out_of_range(self, client):
        resp = await client.post(
            "/api/rooms",
            json={"name": "Room", "thermostat_entity_id": "climate.x", "temp_offset": 25},
        )
        assert resp.status == 400

    async def test_update_room_invalid_holdover_negative(self, client):
        room = await _create_room(client)
        resp = await client.put(f"/api/rooms/{room['id']}", json={"presence_holdover_hours": -1})
        assert resp.status == 400

    async def test_update_room_invalid_temp_offset_non_numeric(self, client):
        room = await _create_room(client)
        resp = await client.put(f"/api/rooms/{room['id']}", json={"temp_offset": "bad"})
        assert resp.status == 400

    async def test_update_room_invalid_temp_offset_out_of_range(self, client):
        room = await _create_room(client)
        resp = await client.put(f"/api/rooms/{room['id']}", json={"temp_offset": 25})
        assert resp.status == 400

    # Per-room deadband override (Issue #277)
    async def test_create_room_deadband_override(self, client):
        resp = await client.post(
            "/api/rooms",
            json={
                "name": "Nursery",
                "thermostat_entity_id": "climate.x",
                "deadband_override": 1.5,
            },
        )
        assert resp.status == 201
        assert (await resp.json())["deadband_override"] == 1.5

    async def test_create_room_deadband_override_defaults_none(self, client):
        room = await _create_room(client)
        assert room["deadband_override"] is None

    async def test_create_room_invalid_deadband_override_non_numeric(self, client):
        resp = await client.post(
            "/api/rooms",
            json={"name": "Room", "thermostat_entity_id": "climate.x", "deadband_override": "bad"},
        )
        assert resp.status == 400

    async def test_create_room_invalid_deadband_override_out_of_range(self, client):
        resp = await client.post(
            "/api/rooms",
            json={"name": "Room", "thermostat_entity_id": "climate.x", "deadband_override": 25},
        )
        assert resp.status == 400

    async def test_update_room_deadband_override_set_and_clear(self, client):
        room = await _create_room(client)
        resp = await client.put(f"/api/rooms/{room['id']}", json={"deadband_override": 2.0})
        assert resp.status == 200
        assert (await resp.json())["deadband_override"] == 2.0
        # Null clears the override, restoring inheritance.
        resp = await client.put(f"/api/rooms/{room['id']}", json={"deadband_override": None})
        assert resp.status == 200
        assert (await resp.json())["deadband_override"] is None

    async def test_update_room_invalid_deadband_override_out_of_range(self, client):
        room = await _create_room(client)
        resp = await client.put(f"/api/rooms/{room['id']}", json={"deadband_override": -1})
        assert resp.status == 400


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
        """201/200 alone would also pass against handlers that never touched
        the table, so read the membership list back on both sides."""
        room = await _create_room(client)
        listed = await (await client.get(f"/api/rooms/{room['id']}/sensors")).json()
        assert listed == []

        resp = await client.post(
            f"/api/rooms/{room['id']}/sensors",
            json={"entity_id": "sensor.bedroom_temp"},
        )
        assert resp.status == 201
        listed = await (await client.get(f"/api/rooms/{room['id']}/sensors")).json()
        assert [s["entity_id"] for s in listed] == ["sensor.bedroom_temp"]

        resp = await client.delete(f"/api/rooms/{room['id']}/sensors/sensor.bedroom_temp")
        assert resp.status == 200
        listed = await (await client.get(f"/api/rooms/{room['id']}/sensors")).json()
        assert listed == []


# ---------------------------------------------------------------------------
# Vents
# ---------------------------------------------------------------------------


class TestVents:
    async def test_list_vents(self, client):
        room = await _create_room(client)
        resp = await client.get(f"/api/rooms/{room['id']}/vents")
        assert resp.status == 200
        assert await resp.json() == []  # a fresh room owns no vents yet

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

    # Each control_method × direction must reach a DIFFERENT HA cover service
    # with the right payload. Asserting only `status == 200` cannot tell the
    # branches apart — every one of these would pass if the handler always
    # called open_cover — so pin the exact ServiceCall the dispatch produced.
    @pytest.mark.parametrize(
        ("method", "direction", "seed", "service", "data"),
        [
            ("open_close", "open", "closed", "open_cover", {"entity_id": "cover.v1"}),
            ("open_close", "close", "open", "close_cover", {"entity_id": "cover.v1"}),
            (
                "set_position",
                "open",
                "closed",
                "set_cover_position",
                {"entity_id": "cover.v1", "position": 100},
            ),
            (
                "set_position",
                "close",
                "open",
                "set_cover_position",
                {"entity_id": "cover.v1", "position": 0},
            ),
            (
                "set_tilt_position",
                "open",
                "closed",
                "set_cover_tilt_position",
                {"entity_id": "cover.v1", "tilt_position": 100},
            ),
            (
                "set_tilt_position",
                "close",
                "open",
                "set_cover_tilt_position",
                {"entity_id": "cover.v1", "tilt_position": 0},
            ),
            ("toggle", "open", "closed", "toggle", {"entity_id": "cover.v1"}),
            ("toggle", "close", "open", "toggle", {"entity_id": "cover.v1"}),
        ],
    )
    async def test_vent_test_dispatches_the_right_ha_service(
        self, client, fake_ha, method, direction, seed, service, data
    ):
        fake_ha.seed_state("cover.v1", seed, {})
        fake_ha.reset_calls()
        resp = await client.post(
            "/api/vents/test",
            json={"entity_id": "cover.v1", "control_method": method, "direction": direction},
        )
        assert resp.status == 200, await resp.text()
        assert [(c.domain, c.service, c.data) for c in fake_ha.calls] == [("cover", service, data)]

    async def test_vent_test_returns_400_when_ha_call_raises(self, client, fake_ha, caplog):
        """When the underlying HA service call fails, the handler logs and
        returns a generic 400 — it must not leak the exception detail.

        Both halves are asserted, because the generic body is only defensible
        if the detail survives somewhere the operator can reach (#609b). The
        docstring on ``test_vent`` points at exactly two places — the server
        log and an event-log warning visible on the Logs page — so deleting
        either would make that docstring the next false claim. Asserting only
        the 400 body left both deletable with every test still green.
        """
        fake_ha.seed_state("cover.v1", "closed", {})
        with (
            caplog.at_level(logging.ERROR, logger="backend.api.routes"),
            patch.object(client.app["ha"], "open_cover", side_effect=RuntimeError("HA boom")),
        ):
            resp = await client.post(
                "/api/vents/test",
                json={"entity_id": "cover.v1", "control_method": "open_close", "direction": "open"},
            )
        assert resp.status == 400
        body = await resp.json()
        assert body["error"] == "Vent test failed"
        assert "boom" not in body["error"]  # no raw exception leakage (CWE-209)

        # The traceback IS written server-side, with the exception attached
        # (`log.exception`, not `log.error`) so the cause is diagnosable.
        records = [r for r in caplog.records if "Vent test open failed" in r.getMessage()]
        assert len(records) == 1, [r.getMessage() for r in caplog.records]
        assert records[0].exc_info is not None
        assert "HA boom" in caplog.text

        # …and the warning event reaches the Logs view, carrying the draft form
        # state the user was iterating on.
        events = await (await client.get("/api/logs/events?level=warning")).json()
        matching = [e for e in events if e["message"].startswith("Vent test open failed")]
        assert len(matching) == 1, events
        assert matching[0]["category"] == "api"
        assert "cover.v1" in matching[0]["message"]


# ---------------------------------------------------------------------------
# Presence sensors
# ---------------------------------------------------------------------------


class TestPresenceSensors:
    async def test_list_presence(self, client):
        room = await _create_room(client)
        resp = await client.get(f"/api/rooms/{room['id']}/presence")
        assert resp.status == 200
        assert await resp.json() == []  # a fresh room owns no presence sensors

    async def test_add_presence_missing_entity_id(self, client):
        room = await _create_room(client)
        resp = await client.post(f"/api/rooms/{room['id']}/presence", json={})
        assert resp.status == 400

    async def test_add_and_remove_presence(self, client):
        """As with sensors, the status codes prove nothing on their own — the
        membership list is the observable that must change."""
        room = await _create_room(client)
        resp = await client.post(
            f"/api/rooms/{room['id']}/presence",
            json={"entity_id": "binary_sensor.presence"},
        )
        assert resp.status == 201
        listed = await (await client.get(f"/api/rooms/{room['id']}/presence")).json()
        assert [p["entity_id"] for p in listed] == ["binary_sensor.presence"]

        resp = await client.delete(f"/api/rooms/{room['id']}/presence/binary_sensor.presence")
        assert resp.status == 200
        listed = await (await client.get(f"/api/rooms/{room['id']}/presence")).json()
        assert listed == []


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

        resp = await client.delete(f"/api/rooms/{room['id']}/schedules/{sched['id']}")
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

    async def test_create_thermostat_missing_total_vents_count(self, client):
        """POST without total_vents_count is rejected — it is mandatory at
        first registration (#213). Existing thermostats fill it in via PUT
        after they upgrade."""
        resp = await client.post(
            "/api/thermostats",
            json={"thermostat_entity_id": "climate.upstairs", "name": "Upstairs"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert "total_vents_count" in data["error"]
        # The error message must explicitly instruct the user to count ALL
        # registers, not only smart vents.
        assert "smart vents AND passive" in data["error"]

    async def test_create_thermostat_invalid_total_vents_count(self, client):
        """total_vents_count must be a positive integer."""
        for bad in (0, -3, "five", 2.5):
            resp = await client.post(
                "/api/thermostats",
                json={
                    "thermostat_entity_id": "climate.bad",
                    "total_vents_count": bad,
                },
            )
            assert resp.status == 400, f"value {bad!r} should be rejected"

    async def test_create_thermostat_invalid_fraction(self, client):
        """min_open_vents_fraction must be 0 < f ≤ 1."""
        for bad in (0, -0.5, 1.5, 2):
            resp = await client.post(
                "/api/thermostats",
                json={
                    "thermostat_entity_id": "climate.bad",
                    "total_vents_count": 6,
                    "min_open_vents_fraction": bad,
                },
            )
            assert resp.status == 400, f"fraction {bad!r} should be rejected"

    # Issue #295: safety-critical numeric fields must be validated, not stored
    # verbatim (a string or negative value otherwise crashes the engine tick).
    _SAFETY_NUMERIC_FIELDS = (
        "deadband",
        "overshoot_delta",
        "max_vent_closed_min",
        "cycle_timeout_hours",
        "reconciliation_interval_min",
        "min_cycle_runtime_min",
        "min_cycle_offtime_min",
    )

    async def test_create_thermostat_rejects_non_numeric_safety_fields(self, client):
        base = {"thermostat_entity_id": "climate.bad", "total_vents_count": 6}
        for field in self._SAFETY_NUMERIC_FIELDS:
            resp = await client.post("/api/thermostats", json={**base, field: "nope"})
            assert resp.status == 400, f"{field}='nope' must be rejected"

    async def test_create_thermostat_rejects_negative_safety_fields(self, client):
        base = {"thermostat_entity_id": "climate.bad", "total_vents_count": 6}
        for field in self._SAFETY_NUMERIC_FIELDS:
            resp = await client.post("/api/thermostats", json={**base, field: -1})
            assert resp.status == 400, f"{field}=-1 must be rejected"

    async def test_create_thermostat_rejects_zero_cycle_timeout(self, client):
        # A zero hard-timeout would instantly time out every cycle.
        resp = await client.post(
            "/api/thermostats",
            json={
                "thermostat_entity_id": "climate.bad",
                "total_vents_count": 6,
                "cycle_timeout_hours": 0,
            },
        )
        assert resp.status == 400

    async def test_update_thermostat_rejects_invalid_safety_fields(self, client):
        await client.post(
            "/api/thermostats",
            json={"thermostat_entity_id": "climate.up", "total_vents_count": 6},
        )
        for field in self._SAFETY_NUMERIC_FIELDS:
            resp = await client.put("/api/thermostats/climate.up", json={field: "nope"})
            assert resp.status == 400, f"PUT {field}='nope' must be rejected"
            resp = await client.put("/api/thermostats/climate.up", json={field: -2})
            assert resp.status == 400, f"PUT {field}=-2 must be rejected"

    async def test_safety_numeric_fields_persist_valid_values(self, client):
        resp = await client.post(
            "/api/thermostats",
            json={
                "thermostat_entity_id": "climate.ok",
                "total_vents_count": 6,
                "deadband": 1.0,
                "overshoot_delta": 1.5,
                "max_vent_closed_min": 10,
                "cycle_timeout_hours": 2.0,
                "reconciliation_interval_min": 5,
                "min_cycle_runtime_min": 3,
                "min_cycle_offtime_min": 4,
            },
        )
        assert resp.status == 201
        data = await resp.json()
        # Default unit is °F, so delta conversion is identity.
        assert data["deadband"] == 1.0
        assert data["overshoot_delta"] == 1.5
        assert data["max_vent_closed_min"] == 10
        assert data["cycle_timeout_hours"] == 2.0
        assert data["reconciliation_interval_min"] == 5
        assert data["min_cycle_runtime_min"] == 3
        assert data["min_cycle_offtime_min"] == 4

    async def test_update_thermostat_null_total_vents_count_clears_it(self, client):
        """`total_vents_count` is mandatory at POST but nullable at PUT: sending
        an explicit null returns the thermostat to the pre-#213 transitional
        default (no declared register count, so the airflow floor falls back to
        "at least one vent open"). A null must CLEAR the stored value, not be
        rejected by the positive-integer guard and not be silently ignored.
        """
        resp = await client.post(
            "/api/thermostats",
            json={"thermostat_entity_id": "climate.clearable", "total_vents_count": 9},
        )
        assert resp.status == 201
        assert (await resp.json())["total_vents_count"] == 9

        resp = await client.put(
            "/api/thermostats/climate.clearable", json={"total_vents_count": None}
        )
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["total_vents_count"] is None

        # And it is cleared in storage, not just in the response body.
        listed = await (await client.get("/api/thermostats")).json()
        stored = next(t for t in listed if t["thermostat_entity_id"] == "climate.clearable")
        assert stored["total_vents_count"] is None

    async def test_create_thermostat_airflow_fields_persist(self, client):
        """All three airflow fields round-trip through POST → GET."""
        resp = await client.post(
            "/api/thermostats",
            json={
                "thermostat_entity_id": "climate.airflow_ok",
                "total_vents_count": 12,
                "has_bypass_damper": True,
                "min_open_vents_fraction": 0.5,
            },
        )
        assert resp.status == 201
        listing = await (await client.get("/api/thermostats")).json()
        entry = next(t for t in listing if t["thermostat_entity_id"] == "climate.airflow_ok")
        assert entry["total_vents_count"] == 12
        assert entry["has_bypass_damper"] is True
        assert entry["min_open_vents_fraction"] == 0.5

    async def test_create_thermostat(self, client):
        resp = await client.post(
            "/api/thermostats",
            json={
                "thermostat_entity_id": "climate.upstairs",
                "name": "Upstairs",
                "total_vents_count": 6,
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["thermostat_entity_id"] == "climate.upstairs"

    async def test_delete_thermostat(self, client):
        # total_vents_count is mandatory at registration (#213) — without it the
        # POST 400s and the DELETE below would be deleting nothing, so the test
        # would pass even if delete_thermostat were a no-op.
        created = await client.post(
            "/api/thermostats",
            json={"thermostat_entity_id": "climate.main", "total_vents_count": 4},
        )
        assert created.status == 201, await created.text()
        listing = await (await client.get("/api/thermostats")).json()
        assert [t["thermostat_entity_id"] for t in listing] == ["climate.main"]

        resp = await client.delete("/api/thermostats/climate.main")
        assert resp.status == 200
        data = await resp.json()
        assert data["deleted"] == "climate.main"
        assert await (await client.get("/api/thermostats")).json() == []

    # These three bodies must carry total_vents_count: its mandatory-at-
    # registration guard (#213) runs BEFORE any temperature check, so without it
    # the 400 comes from the missing count and the setpoint branch under test is
    # never reached. Assert the exact message so that can't silently recur.
    async def test_create_thermostat_invalid_setpoint_non_numeric(self, client):
        resp = await client.post(
            "/api/thermostats",
            json={
                "thermostat_entity_id": "climate.x",
                "total_vents_count": 6,
                "min_setpoint": "bad",
            },
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "Temperatures must be numeric"

    async def test_create_thermostat_setpoint_out_of_range(self, client):
        resp = await client.post(
            "/api/thermostats",
            json={
                "thermostat_entity_id": "climate.x",
                "total_vents_count": 6,
                "min_setpoint": 30,
            },
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "Setpoints must be between 40.0 and 100.0°F"

    async def test_create_thermostat_min_exceeds_max(self, client):
        resp = await client.post(
            "/api/thermostats",
            json={
                "thermostat_entity_id": "climate.x",
                "total_vents_count": 6,
                "min_setpoint": 80,
                "max_setpoint": 70,
            },
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "min_setpoint must be less than max_setpoint"

    async def test_upsert_thermostat_invalid_setpoint_non_numeric(self, client):
        resp = await client.put(
            "/api/thermostats/climate.x",
            json={"max_setpoint": "bad"},
        )
        assert resp.status == 400

    async def test_upsert_thermostat_min_exceeds_max(self, client):
        resp = await client.put(
            "/api/thermostats/climate.x",
            json={"min_setpoint": 80, "max_setpoint": 70},
        )
        assert resp.status == 400

    async def test_upsert_thermostat_default_temp_null(self, client):
        """Regression: PUT with default_temp=null must not 500 (#231 follow-up).

        Newly-registered thermostats have default_temp=NULL in the DB
        (the Register modal doesn't ask for it). The frontend spreads the
        full config into the PUT body, so `{"default_temp": null}` is the
        normal payload. The handler used to call `_to_f(None, unit)`
        unconditionally, which crashed with TypeError → 500. Mirror the
        cooling_lockout_below_f null-safe pattern.
        """
        resp = await client.put(
            "/api/thermostats/climate.nulldef",
            json={
                "name": "T",
                "default_temp": None,
                "min_setpoint": 62,
                "max_setpoint": 78,
            },
        )
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["default_temp"] is None
        assert data["min_setpoint"] == 62.0
        assert data["max_setpoint"] == 78.0

    async def test_upsert_thermostat_invalid_total_vents_count(self, client):
        resp = await client.put(
            "/api/thermostats/climate.x",
            json={"total_vents_count": 0},
        )
        assert resp.status == 400
        assert "total_vents_count" in (await resp.json())["error"]

    async def test_upsert_thermostat_clears_total_vents_count_with_null(self, client):
        # First register with a count, then PUT null to clear it.
        await client.post(
            "/api/thermostats",
            json={"thermostat_entity_id": "climate.clearvents", "total_vents_count": 8},
        )
        resp = await client.put(
            "/api/thermostats/climate.clearvents",
            json={"total_vents_count": None},
        )
        assert resp.status == 200
        assert (await resp.json())["total_vents_count"] is None

    async def test_upsert_thermostat_invalid_fraction(self, client):
        resp = await client.put(
            "/api/thermostats/climate.x",
            json={"min_open_vents_fraction": 1.5},
        )
        assert resp.status == 400
        assert "min_open_vents_fraction" in (await resp.json())["error"]

    async def test_upsert_thermostat_airflow_fields_persist(self, client):
        await client.post(
            "/api/thermostats",
            json={"thermostat_entity_id": "climate.af", "total_vents_count": 4},
        )
        resp = await client.put(
            "/api/thermostats/climate.af",
            json={
                "has_bypass_damper": True,
                "min_open_vents_fraction": 0.25,
                "overflow_during_min_runtime": False,
            },
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["has_bypass_damper"] is True
        assert data["min_open_vents_fraction"] == 0.25
        assert data["overflow_during_min_runtime"] is False


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
        """DELETE must actually drop the hold — and must stay idempotent when
        there is nothing to drop (the Rooms page fires it unconditionally).
        Asserting only the 200 would pass against a handler that deleted
        nothing at all."""
        room = await _create_room(client)

        resp = await client.post(
            f"/api/rooms/{room['id']}/override",
            json={"target_temp": 70, "duration_hours": 1.0},
        )
        assert resp.status in (200, 201), await resp.text()
        live = await (await client.get("/api/overrides")).json()
        assert [o["room_id"] for o in live] == [room["id"]]

        resp = await client.delete(f"/api/rooms/{room['id']}/override")
        assert resp.status == 200
        assert await (await client.get("/api/overrides")).json() == []

        # Idempotent: clearing again is still a 200, not a 404.
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
        resp = await client.post("/api/rooms/active-status", json={"room_ids": ["nonexistent"]})
        assert resp.status == 200
        assert await resp.json() == {}

    async def test_active_status_known_room(self, client):
        room = await _create_room(client)
        resp = await client.post("/api/rooms/active-status", json={"room_ids": [room["id"]]})
        assert resp.status == 200
        data = await resp.json()
        assert room["id"] in data


# ---------------------------------------------------------------------------
# HA proxy endpoints
# ---------------------------------------------------------------------------


class TestHaProxy:
    async def test_ha_states_entity_not_in_cache(self, client):
        resp = await client.post("/api/ha/states", json={"entity_ids": ["sensor.missing"]})
        assert resp.status == 200
        data = await resp.json()
        assert data["sensor.missing"] is None

    async def test_ha_states_numeric_entity(self, client, fake_ha):
        fake_ha.seed_state("sensor.temp", "72.5", {"unit_of_measurement": "°F"})
        resp = await client.post("/api/ha/states", json={"entity_ids": ["sensor.temp"]})
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
        resp = await client.post("/api/ha/states", json={"entity_ids": ["binary_sensor.door"]})
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

    async def test_overflow_rooms_surface_in_detail_and_list(self, client):
        """Issue #254: overflow rooms must appear in the cycle detail tagged
        role='overflow' (with a name fallback) and the list must flag the
        cycle with had_overflow=True."""
        import json
        from datetime import UTC, datetime, timedelta

        from backend.models import CycleLog, Room, RoomCycleState

        conn = client.app["scheduler"]._db_conn

        active = Room.create(name="Living Room", thermostat_entity_id="climate.main")
        active.id = "r_active"
        await db.upsert_room(conn, active)
        overflow_room = Room.create(name="Office", thermostat_entity_id="climate.main")
        overflow_room.id = "r_overflow"
        await db.upsert_room(conn, overflow_room)

        started = datetime.now(UTC) - timedelta(hours=1)
        cycle = CycleLog.create(
            thermostat_entity_id="climate.main",
            mode="cooling",
            # Only the active room is in the snapshot — overflow rooms are not.
            rooms_json=json.dumps({"r_active": {"name": "Living Room", "target": 72.0}}),
        )
        cycle.started_at = started
        await db.insert_cycle_log(conn, cycle)
        await db.upsert_room_cycle_state(
            conn,
            RoomCycleState(
                cycle_id=cycle.id,
                room_id="r_active",
                target_temp=72.0,
                temp_at_start=78.0,
                temp_at_end=72.0,
            ),
        )
        await db.upsert_room_cycle_state(
            conn,
            RoomCycleState(
                cycle_id=cycle.id,
                room_id="r_overflow",
                target_temp=70.0,
                temp_at_start=75.0,
                temp_at_end=71.0,
                trigger_detail=json.dumps({"overflow": True, "tier": 1}),
                role="overflow",
            ),
        )
        await db.close_cycle_log(conn, cycle.id, started + timedelta(minutes=20))

        # Detail: overflow room present, tagged, name resolved from live room.
        resp = await client.get(f"/api/logs/{cycle.id}/detail")
        assert resp.status == 200
        detail = await resp.json()
        assert detail["cycle"]["had_overflow"] is True
        by_id = {r["room_id"]: r for r in detail["rooms"]}
        assert by_id["r_active"]["role"] == "active"
        ov = by_id["r_overflow"]
        assert ov["role"] == "overflow"
        assert ov["name"] == "Office"  # fallback to live room name
        assert ov["temp_at_start"] == 75.0
        assert ov["temp_at_end"] == 71.0

        # List: the cycle is flagged so the UI can render a badge.
        resp = await client.get("/api/logs")
        assert resp.status == 200
        logs = await resp.json()
        flagged = {log_["id"]: log_["had_overflow"] for log_ in logs}
        assert flagged.get(cycle.id) is True

    async def test_cycle_without_overflow_not_flagged(self, client):
        """A normal cycle must report had_overflow=False in both list and detail."""
        await _seed_completed_cycle(client)
        resp = await client.get("/api/logs")
        logs = await resp.json()
        assert logs
        assert all(log_["had_overflow"] is False for log_ in logs)


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
        assert "mcp_enabled" in data
        # MCP defaults to off (opt-in).
        assert data["mcp_enabled"] is False

    async def test_set_mcp_enabled(self, client):
        resp = await client.post("/api/system/mcp", json={"mcp_enabled": True})
        assert resp.status == 200
        assert (await resp.json())["mcp_enabled"] is True
        # Persisted + reflected in status.
        assert client.app["scheduler"].get_mcp_enabled() is True
        status = await (await client.get("/api/system/status")).json()
        assert status["mcp_enabled"] is True

    async def test_set_mcp_enabled_missing_field(self, client):
        resp = await client.post("/api/system/mcp", json={})
        assert resp.status == 400

    async def test_theme_defaults_to_system(self, client):
        resp = await client.get("/api/settings")
        assert resp.status == 200
        assert (await resp.json())["theme"] == "system"

    async def test_set_theme(self, client):
        resp = await client.post("/api/settings/theme", json={"theme": "dark"})
        assert resp.status == 200
        assert (await resp.json())["theme"] == "dark"
        # Persisted in scheduler state + reflected in the settings aggregate.
        assert client.app["scheduler"].get_theme() == "dark"
        settings = await (await client.get("/api/settings")).json()
        assert settings["theme"] == "dark"

    async def test_set_theme_rejects_invalid(self, client):
        for bad in ("blue", "", None):
            resp = await client.post("/api/settings/theme", json={"theme": bad})
            assert resp.status == 400

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

    async def test_backup_does_not_leak_temp_file(self, client, tmp_path, monkeypatch):
        """Issue #298: the temporary DB snapshot must be deleted after the
        response — a backup download must not leave a full copy of the database
        (rooms, schedules, settings, …) on disk indefinitely."""
        import tempfile as _tempfile

        backup_dir = tmp_path / "backuptmp"
        backup_dir.mkdir()
        monkeypatch.setattr(_tempfile, "tempdir", str(backup_dir))

        resp = await client.get("/api/backup")
        assert resp.status == 200
        body = await resp.read()
        assert body[:16] == b"SQLite format 3\x00"

        leftover = list(backup_dir.glob("*.db"))
        assert leftover == [], f"backup leaked temp snapshot(s): {leftover}"

    async def test_restore_non_sqlite_file_rejected(self, client):
        data = io.BytesIO(b"not a sqlite database content here!!")
        form_data = {"file": data}
        resp = await client.post("/api/restore", data=form_data)
        assert resp.status == 400
        body = await resp.json()
        # Pin the REASON, not just the 400: a missing-field or oversize
        # rejection also 400s, and this test must fail if the SQLite header
        # sniff stops running rather than passing on some other error.
        assert body["error"] == "Uploaded file is not a valid SQLite database"

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


# ---------------------------------------------------------------------------
# Settings — temperature unit endpoints (Issue #123)
# ---------------------------------------------------------------------------


class TestTemperatureUnitSettings:
    async def test_get_settings_returns_temperature_unit(self, client):
        resp = await client.get("/api/settings")
        assert resp.status == 200
        data = await resp.json()
        # Pin the value, not merely membership in the enum: `in ("F", "C")`
        # holds whichever unit is returned, so it cannot catch the endpoint
        # reading the wrong source. The unit must be the one the scheduler
        # resolved (the test stack boots in °F).
        assert data["temperature_unit"] == client.app["scheduler"].get_temperature_unit() == "F"
        assert data["unit_change_ack_required"] is False

    async def test_get_settings_ack_required_false_initially(self, client):
        resp = await client.get("/api/settings")
        data = await resp.json()
        assert data["unit_change_ack_required"] is False

    async def test_ack_unit_change_clears_flag(self, client):
        scheduler = client.app["scheduler"]
        await db.set_system_setting(scheduler._db_conn, "unit_change_ack_required", "1")
        resp = await client.post("/api/settings/ack-unit-change")
        assert resp.status == 200
        data = await resp.json()
        assert data["unit_change_ack_required"] is False
        resp2 = await client.get("/api/settings")
        assert (await resp2.json())["unit_change_ack_required"] is False

    async def test_restart_returns_restarting(self, client):
        with patch("backend.api.routes.os.kill") as mock_kill:
            resp = await client.post("/api/restart")
            assert resp.status == 200
            data = await resp.json()
            assert data["restarting"] is True
            await asyncio.sleep(0.5)
            mock_kill.assert_called_once_with(os.getpid(), signal.SIGTERM)


# ---------------------------------------------------------------------------
# Metrics CSV export (2i)
# ---------------------------------------------------------------------------


async def _seed_completed_cycle(client, *, mode: str = "cooling") -> None:
    """Insert one completed cycle log with temps so CSV export has a row."""
    import json
    from datetime import UTC, datetime, timedelta

    from backend.models import CycleLog

    conn = client.app["scheduler"]._db_conn
    started = datetime.now(UTC) - timedelta(hours=1)
    cycle = CycleLog.create(
        thermostat_entity_id="climate.main",
        mode=mode,
        rooms_json=json.dumps({"r1": {"name": "Room", "target": 72.0}}),
    )
    cycle.started_at = started
    cycle.thermostat_temp_at_start = 78.0
    cycle.setpoint_at_start = 72.0
    cycle.outside_temp_at_start = 90.0
    await db.insert_cycle_log(conn, cycle)
    await db.close_cycle_log(
        conn,
        cycle.id,
        started + timedelta(minutes=30),
        thermostat_temp_at_end=72.0,
    )


class TestMetricsExportCsv:
    async def test_home_scope_returns_csv_with_header_and_row(self, client):
        await _seed_completed_cycle(client)
        resp = await client.get("/api/metrics/export.csv?scope=home")
        assert resp.status == 200
        assert resp.content_type == "text/csv"
        assert "attachment" in resp.headers["Content-Disposition"]
        text = await resp.text()
        lines = text.strip().splitlines()
        # Header labels the temperature columns with the active unit (°F default).
        assert "thermostat_temp_at_start (°F)" in lines[0]
        # Our seeded cycle is present, with a computed duration.
        assert "climate.main" in text
        assert any(",1800," in ln for ln in lines[1:])

    async def test_thermostat_scope_requires_entity_id(self, client):
        resp = await client.get("/api/metrics/export.csv?scope=thermostat")
        assert resp.status == 400
        assert "entity_id" in (await resp.json())["error"]

    async def test_invalid_scope_rejected(self, client):
        resp = await client.get("/api/metrics/export.csv?scope=bogus")
        assert resp.status == 400

    async def test_thermostat_scope_filters_to_entity(self, client):
        await _seed_completed_cycle(client)
        resp = await client.get("/api/metrics/export.csv?scope=thermostat&entity_id=climate.main")
        assert resp.status == 200
        assert "climate.main" in await resp.text()

    async def test_celsius_unit_labels_header(self, client):
        client.app["scheduler"]._active_unit = "C"
        try:
            resp = await client.get("/api/metrics/export.csv?scope=home")
            assert resp.status == 200
            assert "thermostat_temp_at_start (°C)" in (await resp.text()).splitlines()[0]
        finally:
            client.app["scheduler"]._active_unit = "F"


# ---------------------------------------------------------------------------
# Celsius fixture + route-level conversion integration tests (Issue #123 Phase 2)
# ---------------------------------------------------------------------------


@pytest.fixture
async def celsius_client(fake_ha, db_path):
    """Client where the active temperature unit has been set to Celsius."""
    app = build_app(fake_ha, db_path, frontend_dist=None, start_ha=False)
    server = TestServer(app)
    async with TestClient(server) as c:
        await c.start_server()
        c.app["scheduler"]._active_unit = "C"
        yield c


class TestRoomTempConversion:
    async def test_create_room_system_wide_temp_celsius(self, celsius_client):
        resp = await celsius_client.post(
            "/api/rooms",
            json={
                "name": "Bedroom",
                "thermostat_entity_id": "climate.test",
                "system_wide_temp": 20.0,
            },
        )
        assert resp.status == 201
        assert (await resp.json())["system_wide_temp"] == 68.0  # 20°C → 68°F

    async def test_celsius_form_bounds_are_accepted_by_the_write_boundary(self, celsius_client):
        """The °C bounds the forms advertise must actually save (#521).

        Converting a °F limit for display rounds, and rounding can move the
        bound OUTWARD: toDisplay(40) is 4.4 °C, which converts back to 39.92 °F
        and fails the 40-90 check, so a form comparing against the raw
        conversion advertises a minimum it then refuses. The frontend's
        displayBound() nudges inward to 4.5 / 32.2 / 5.55; this pins that those
        three values are ones this boundary really accepts, so the two sides
        cannot drift apart.
        """
        resp = await celsius_client.post(
            "/api/rooms",
            json={
                "name": "Bounds",
                "thermostat_entity_id": "climate.test",
                "system_wide_temp": 4.5,
                "deadband_override": 5.55,
            },
        )
        assert resp.status == 201, await resp.text()
        body = await resp.json()
        assert body["system_wide_temp"] == 40.1
        assert body["deadband_override"] == 9.99

        resp = await celsius_client.post(
            "/api/rooms",
            json={
                "name": "Bounds max",
                "thermostat_entity_id": "climate.test",
                "system_wide_temp": 32.2,
            },
        )
        assert resp.status == 201, await resp.text()
        assert (await resp.json())["system_wide_temp"] == 89.96

    async def test_the_naive_display_bounds_are_the_ones_that_would_fail(self, celsius_client):
        """Guards the premise of #521 rather than the fix.

        4.4 °C and 5.56 °C are what a raw toDisplay/toDisplayDelta of the °F
        limits produces. If this boundary ever starts accepting them, the
        inward nudge in displayBound() is no longer needed and should go —
        this test is what will say so.
        """
        resp = await celsius_client.post(
            "/api/rooms",
            json={
                "name": "Naive min",
                "thermostat_entity_id": "climate.test",
                "system_wide_temp": 4.4,
            },
        )
        assert resp.status == 400, "4.4 °C is 39.92 °F — below the 40 °F floor"

        resp = await celsius_client.post(
            "/api/rooms",
            json={
                "name": "Naive band",
                "thermostat_entity_id": "climate.test",
                "deadband_override": 5.56,
            },
        )
        assert resp.status == 400, "5.56 °C is 10.01 °F — above the 10 °F ceiling"

    async def test_create_room_system_wide_temp_fahrenheit(self, client):
        resp = await client.post(
            "/api/rooms",
            json={
                "name": "Bedroom",
                "thermostat_entity_id": "climate.test",
                "system_wide_temp": 70.0,
            },
        )
        assert resp.status == 201
        assert (await resp.json())["system_wide_temp"] == 70.0

    async def test_update_room_system_wide_temp_celsius(self, celsius_client):
        r = await celsius_client.post(
            "/api/rooms",
            json={"name": "Den", "thermostat_entity_id": "climate.test"},
        )
        room_id = (await r.json())["id"]
        resp = await celsius_client.put(
            f"/api/rooms/{room_id}",
            json={"system_wide_temp": 21.0},
        )
        assert resp.status == 200
        assert (await resp.json())["system_wide_temp"] == 69.8  # 21°C → 69.8°F

    async def test_create_room_temp_offset_celsius(self, celsius_client):
        resp = await celsius_client.post(
            "/api/rooms",
            json={"name": "Office", "thermostat_entity_id": "climate.test", "temp_offset": 1.0},
        )
        assert resp.status == 201
        assert (await resp.json())["temp_offset"] == 1.8  # 1°C delta → 1.8°F

    async def test_update_room_temp_offset_delta_celsius(self, celsius_client):
        r = await celsius_client.post(
            "/api/rooms",
            json={"name": "Den", "thermostat_entity_id": "climate.test"},
        )
        room_id = (await r.json())["id"]
        resp = await celsius_client.put(
            f"/api/rooms/{room_id}",
            json={"temp_offset": 1.0},
        )
        assert resp.status == 200
        assert (await resp.json())["temp_offset"] == 1.8  # 1°C delta → 1.8°F

    async def test_create_room_deadband_override_celsius(self, celsius_client):
        # Per-room deadband override is a delta (Issue #277) — 1°C → 1.8°F, no
        # -32 offset. The frontend sends the raw °C value; the backend converts.
        resp = await celsius_client.post(
            "/api/rooms",
            json={
                "name": "Office",
                "thermostat_entity_id": "climate.test",
                "deadband_override": 1.0,
            },
        )
        assert resp.status == 201
        assert (await resp.json())["deadband_override"] == 1.8  # 1°C delta → 1.8°F

    async def test_update_room_deadband_override_delta_celsius(self, celsius_client):
        r = await celsius_client.post(
            "/api/rooms",
            json={"name": "Den", "thermostat_entity_id": "climate.test"},
        )
        room_id = (await r.json())["id"]
        resp = await celsius_client.put(
            f"/api/rooms/{room_id}",
            json={"deadband_override": 1.0},
        )
        assert resp.status == 200
        assert (await resp.json())["deadband_override"] == 1.8  # 1°C delta → 1.8°F

    async def test_update_room_system_wide_temp_none(self, celsius_client):
        r = await celsius_client.post(
            "/api/rooms",
            json={"name": "Den", "thermostat_entity_id": "climate.test"},
        )
        room_id = (await r.json())["id"]
        resp = await celsius_client.put(
            f"/api/rooms/{room_id}",
            json={"system_wide_temp": None},
        )
        assert resp.status == 200
        assert (await resp.json())["system_wide_temp"] is None


class TestScheduleTempConversion:
    async def _make_room(self, client):
        r = await client.post(
            "/api/rooms",
            json={"name": "Office", "thermostat_entity_id": "climate.test"},
        )
        return (await r.json())["id"]

    async def test_create_schedule_target_temp_celsius(self, celsius_client):
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
        assert (await resp.json())["target_temp"] == 68.0

    async def test_create_schedule_precision_celsius(self, celsius_client):
        room_id = await self._make_room(celsius_client)
        resp = await celsius_client.post(
            f"/api/rooms/{room_id}/schedules",
            json={
                "days_of_week": [5, 6],
                "start_time": "10:00",
                "end_time": "20:00",
                "target_temp": 6.3,
            },
        )
        assert resp.status == 201
        assert (await resp.json())["target_temp"] == 43.34  # 2dp precision

    async def test_update_schedule_target_temp_celsius(self, celsius_client):
        room_id = await self._make_room(celsius_client)
        r2 = await celsius_client.post(
            f"/api/rooms/{room_id}/schedules",
            json={
                "days_of_week": [0],
                "start_time": "08:00",
                "end_time": "18:00",
                "target_temp": 21.0,
            },
        )
        sched_id = (await r2.json())["id"]
        resp = await celsius_client.put(
            f"/api/rooms/{room_id}/schedules/{sched_id}",
            json={"target_temp": 22.0},
        )
        assert resp.status == 200
        assert (await resp.json())["target_temp"] == 71.6  # 22°C → 71.6°F

    # Per-schedule deadband override (Issue #517) — a DELTA, so 1°C → 1.8°F
    # with NO -32 offset. The frontend sends the raw °C the user typed; the
    # backend's `_delta_to_f` is the one and only conversion (#231).

    async def _make_block(self, client, room_id, **overrides):
        body = {
            "days_of_week": [0, 1, 2, 3, 4],
            "start_time": "08:00",
            "end_time": "18:00",
            "target_temp": 20.0,
        }
        body.update(overrides)
        resp = await client.post(f"/api/rooms/{room_id}/schedules", json=body)
        return resp.status, (await resp.json())

    async def test_create_schedule_deadband_override_is_a_delta_celsius(self, celsius_client):
        room_id = await self._make_room(celsius_client)
        status, body = await self._make_block(celsius_client, room_id, deadband_override=1.0)
        assert status == 201, body
        assert body["deadband_override"] == 1.8  # 1°C delta → 1.8°F
        # #284 guard: an ABSOLUTE conversion would have stored 33.8°F. If this
        # field ever regresses from `_delta_to_f` to `_to_f`, this fails loudly.
        assert body["deadband_override"] != 33.8

    async def test_update_schedule_deadband_override_is_a_delta_celsius(self, celsius_client):
        room_id = await self._make_room(celsius_client)
        _, block = await self._make_block(celsius_client, room_id)
        resp = await celsius_client.put(
            f"/api/rooms/{room_id}/schedules/{block['id']}",
            json={"deadband_override": 1.0},
        )
        assert resp.status == 200, await resp.text()
        updated = await resp.json()
        assert updated["deadband_override"] == 1.8
        assert updated["deadband_override"] != 33.8  # absolute conversion guard

    async def test_schedule_deadband_override_get_echoes_raw_fahrenheit(self, celsius_client):
        """Storage and the read boundary are always °F — only the frontend
        display layer converts back to °C."""
        room_id = await self._make_room(celsius_client)
        _, block = await self._make_block(celsius_client, room_id, deadband_override=2.0)
        got = await (await celsius_client.get(f"/api/rooms/{room_id}/schedules")).json()
        assert got[0]["deadband_override"] == 3.6  # 2°C delta → 3.6°F, echoed raw
        assert block["deadband_override"] == 3.6

    async def test_schedule_deadband_override_zero_celsius(self, celsius_client):
        """0°C is 0°F as a delta (an absolute conversion would give 32°F)."""
        room_id = await self._make_room(celsius_client)
        _, body = await self._make_block(celsius_client, room_id, deadband_override=0.0)
        assert body["deadband_override"] == 0.0

    async def test_schedule_deadband_override_bounds_are_checked_after_conversion(
        self, celsius_client
    ):
        """The 0–10 °F bound applies to the CONVERTED value: 5.5°C → 9.9°F is
        accepted, 6°C → 10.8°F is not."""
        room_id = await self._make_room(celsius_client)
        status, body = await self._make_block(celsius_client, room_id, deadband_override=5.5)
        assert status == 201, body
        assert body["deadband_override"] == 9.9

        status, body = await self._make_block(
            celsius_client, room_id, start_time="19:00", end_time="20:00", deadband_override=6.0
        )
        assert status == 400, body

    async def test_schedule_deadband_override_celsius_ceiling_is_5_55_not_5_56(
        self, celsius_client
    ):
        """Pins the exact °C boundary the UI has to advertise.

        10 °F is 5.5555… °C. Rounded to the 2dp the UI works in that is 5.56,
        which converts BACK to 10.01 °F and is refused — so 5.56 is not a
        usable maximum even though it looks like one. 5.55 → 9.99 °F is. The
        Schedules modal caps its input at 5.55 for exactly this reason; if this
        assertion ever flips, that cap has to move with it.
        """
        room_id = await self._make_room(celsius_client)

        status, body = await self._make_block(celsius_client, room_id, deadband_override=5.56)
        assert status == 400, f"5.56°C converts to 10.01°F and must be refused: {body}"

        status, body = await self._make_block(
            celsius_client, room_id, start_time="19:00", end_time="20:00", deadband_override=5.55
        )
        assert status == 201, body
        assert body["deadband_override"] == 9.99

    async def test_copy_schedule_carries_the_converted_band_celsius(self, celsius_client):
        """The copy replicates the STORED °F value — it must not re-convert."""
        src = await self._make_room(celsius_client)
        r = await celsius_client.post(
            "/api/rooms",
            json={"name": "Den", "thermostat_entity_id": "climate.test"},
        )
        dst = (await r.json())["id"]
        _, block = await self._make_block(celsius_client, src, deadband_override=1.0)
        resp = await celsius_client.post(
            f"/api/rooms/{src}/schedules/{block['id']}/copy",
            json={"target_room_ids": [dst]},
        )
        assert resp.status == 200, await resp.text()
        got = await (await celsius_client.get(f"/api/rooms/{dst}/schedules")).json()
        assert got[0]["deadband_override"] == 1.8  # still 1.8°F, not 3.24°F


class TestThermostatTempConversion:
    async def test_create_thermostat_absolute_fields_celsius(self, celsius_client):
        resp = await celsius_client.post(
            "/api/thermostats",
            json={
                "thermostat_entity_id": "climate.conv1",
                "total_vents_count": 6,
                "default_temp": 20.0,
                "min_setpoint": 16.0,
                "max_setpoint": 26.0,
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["default_temp"] == 68.0
        assert data["min_setpoint"] == 60.8
        assert data["max_setpoint"] == 78.8

    async def test_create_thermostat_delta_fields_celsius(self, celsius_client):
        resp = await celsius_client.post(
            "/api/thermostats",
            json={
                "thermostat_entity_id": "climate.conv2",
                "total_vents_count": 6,
                "deadband": 0.5,
                "overshoot_delta": 1.0,
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["deadband"] == 0.9
        assert data["overshoot_delta"] == 1.8

    async def test_upsert_thermostat_celsius(self, celsius_client):
        resp = await celsius_client.put(
            "/api/thermostats/climate.conv3",
            json={"min_setpoint": 18.0, "overshoot_delta": 2.0},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["min_setpoint"] == 64.4
        assert data["overshoot_delta"] == 3.6

    async def test_thermostat_no_conversion_fahrenheit(self, client):
        resp = await client.post(
            "/api/thermostats",
            json={
                "thermostat_entity_id": "climate.conv4",
                "total_vents_count": 6,
                "default_temp": 70.0,
                "deadband": 0.5,
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["default_temp"] == 70.0
        assert data["deadband"] == 0.5


class TestOverrideTempConversion:
    async def test_override_target_temp_celsius(self, celsius_client):
        r = await celsius_client.post(
            "/api/rooms",
            json={"name": "Sunroom", "thermostat_entity_id": "climate.test"},
        )
        room_id = (await r.json())["id"]
        resp = await celsius_client.post(
            f"/api/rooms/{room_id}/override",
            json={"target_temp": 22.0, "duration_hours": 1},
        )
        assert resp.status == 200
        assert (await resp.json())["target_temp"] == 71.6  # 22°C → 71.6°F

    async def test_override_target_temp_fahrenheit(self, client):
        r = await client.post(
            "/api/rooms",
            json={"name": "Sunroom", "thermostat_entity_id": "climate.test"},
        )
        room_id = (await r.json())["id"]
        resp = await client.post(
            f"/api/rooms/{room_id}/override",
            json={"target_temp": 72.0, "duration_hours": 1},
        )
        assert resp.status == 200
        assert (await resp.json())["target_temp"] == 72.0
