"""Coverage-closing integration tests for ``backend/api/routes.py`` (lines < 2000).

Every test here drives a real HTTP request through the full aiohttp app and
asserts on the response body, not just the status code. Error branches assert
the exact user-facing message so a message change (or a validation branch
silently moving) fails loudly instead of passing on an incidental 400.
"""

from __future__ import annotations

import pytest

OUTDOOR = "sensor.outdoor"


async def _create_room(client, name="Living Room", thermostat="climate.test"):
    resp = await client.post(
        "/api/rooms",
        json={"name": name, "thermostat_entity_id": thermostat},
    )
    assert resp.status == 201, await resp.text()
    return await resp.json()


async def _configure_outside_sensor(client, fake_ha, temp_f: float = 75.0) -> None:
    """Register the house-wide outside-temperature entity (Issue #85 Phase 1b).

    Since #524 this is a precondition for *enabling* ambient suppression, so
    tests that need to get past ``_ambient_enable_blocked`` and reach the
    per-field validation must do this first.
    """
    fake_ha.seed_state(OUTDOOR, str(temp_f), {"unit_of_measurement": "°F"})
    resp = await client.put("/api/settings/outside-temp-entity", json={"entity_id": OUTDOOR})
    assert resp.status == 200, await resp.text()


# ---------------------------------------------------------------------------
# Room name sanitisation (Issue #519) — the blank-after-strip branch
# ---------------------------------------------------------------------------


class TestRoomNameBlank:
    async def test_create_room_whitespace_only_name_rejected(self, client):
        """``"   "`` is truthy, so it clears the ``not body.get("name")`` gate and
        must be caught by the strip() check inside ``_room_name_rejected``."""
        resp = await client.post(
            "/api/rooms",
            json={"name": "   ", "thermostat_entity_id": "climate.x"},
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "name and thermostat_entity_id required"
        # Nothing was created by the rejected request.
        assert await (await client.get("/api/rooms")).json() == []

    async def test_update_room_whitespace_only_name_rejected(self, client):
        room = await _create_room(client, name="Keeper")
        resp = await client.put(f"/api/rooms/{room['id']}", json={"name": "\t \n"})
        assert resp.status == 400
        assert (await resp.json())["error"] == "name and thermostat_entity_id required"
        # The rename was rejected before anything was applied.
        detail = await (await client.get(f"/api/rooms/{room['id']}")).json()
        assert detail["name"] == "Keeper"

    async def test_create_room_punctuation_only_name_rejected(self, client):
        """A name that sanitises to the empty key is a different branch from a
        blank one: it survives strip() but cannot address a room on MQTT."""
        resp = await client.post(
            "/api/rooms",
            json={"name": "--- !!! ---", "thermostat_entity_id": "climate.x"},
        )
        assert resp.status == 400
        assert "at least one letter or number" in (await resp.json())["error"]


# ---------------------------------------------------------------------------
# Ambient suppression field validation (Issue #248 / #524)
# ---------------------------------------------------------------------------


class TestAmbientSuppressionValidation:
    async def test_enabled_must_be_a_boolean(self, client, fake_ha):
        """A truthy non-bool has to get past ``_ambient_enable_blocked`` first,
        so the outside sensor is configured; the type check is what rejects it."""
        await _configure_outside_sensor(client, fake_ha)
        resp = await client.post(
            "/api/rooms",
            json={
                "name": "Bad",
                "thermostat_entity_id": "climate.x",
                "ambient_suppression_enabled": "yes",
            },
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "ambient_suppression_enabled must be a boolean"
        assert await (await client.get("/api/rooms")).json() == []

    async def test_enabled_falsy_non_bool_also_rejected(self, client):
        """``0`` is falsy so the sensor gate never fires — the type check is the
        only thing standing between it and the DB."""
        resp = await client.post(
            "/api/rooms",
            json={
                "name": "Bad",
                "thermostat_entity_id": "climate.x",
                "ambient_suppression_enabled": 0,
            },
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "ambient_suppression_enabled must be a boolean"

    async def test_min_differential_non_numeric_rejected(self, client):
        resp = await client.post(
            "/api/rooms",
            json={
                "name": "Bad",
                "thermostat_entity_id": "climate.x",
                "ambient_suppression_min_differential": "five",
            },
        )
        assert resp.status == 400
        assert (await resp.json())[
            "error"
        ] == "ambient_suppression_min_differential must be numeric"

    async def test_min_differential_bool_rejected(self, client):
        """``True`` is an ``int`` subclass — the explicit bool guard is what
        stops ``True`` being stored as a 1 °F differential."""
        resp = await client.post(
            "/api/rooms",
            json={
                "name": "Bad",
                "thermostat_entity_id": "climate.x",
                "ambient_suppression_min_differential": True,
            },
        )
        assert resp.status == 400
        assert (await resp.json())[
            "error"
        ] == "ambient_suppression_min_differential must be numeric"

    async def test_deadband_non_numeric_rejected(self, client):
        resp = await client.post(
            "/api/rooms",
            json={
                "name": "Bad",
                "thermostat_entity_id": "climate.x",
                "ambient_suppression_deadband": "wide",
            },
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "ambient_suppression_deadband must be numeric"

    async def test_deadband_bool_rejected(self, client):
        resp = await client.post(
            "/api/rooms",
            json={
                "name": "Bad",
                "thermostat_entity_id": "climate.x",
                "ambient_suppression_deadband": False,
            },
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "ambient_suppression_deadband must be numeric"

    @pytest.mark.parametrize("bad", [-5, 60.5, "60", True])
    async def test_off_schedule_window_must_be_non_negative_int(self, client, bad):
        resp = await client.post(
            "/api/rooms",
            json={
                "name": "Bad",
                "thermostat_entity_id": "climate.x",
                "ambient_suppression_off_schedule_window_min": bad,
            },
        )
        assert resp.status == 400, f"{bad!r} must be rejected"
        assert (await resp.json())["error"] == (
            "ambient_suppression_off_schedule_window_min must be a non-negative integer"
        )

    async def test_off_schedule_window_zero_is_accepted(self, client):
        """0 is the boundary the ``< 0`` check must let through."""
        resp = await client.post(
            "/api/rooms",
            json={
                "name": "Zero",
                "thermostat_entity_id": "climate.x",
                "ambient_suppression_off_schedule_window_min": 0,
            },
        )
        assert resp.status == 201, await resp.text()
        assert (await resp.json())["ambient_suppression_off_schedule_window_min"] == 0

    async def test_update_room_rejects_bad_ambient_field_without_partial_write(self, client):
        """The same helper serves PUT; a rejected ambient field must not let an
        earlier field in the same body through."""
        room = await _create_room(client, name="Keeper")
        resp = await client.put(
            f"/api/rooms/{room['id']}",
            json={"notes": "should not persist", "ambient_suppression_deadband": "wide"},
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "ambient_suppression_deadband must be numeric"
        detail = await (await client.get(f"/api/rooms/{room['id']}")).json()
        assert detail["notes"] == ""


# ---------------------------------------------------------------------------
# MCP token endpoints — malformed JSON bodies
# ---------------------------------------------------------------------------


class TestMcpTokenMalformedJson:
    async def test_create_rejects_malformed_json(self, client):
        resp = await client.post(
            "/api/mcp/tokens",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["error"] == "Invalid JSON body"
        # No token was minted, and no parser detail leaked (CWE-209).
        assert "Expecting" not in body["error"]
        assert await (await client.get("/api/mcp/tokens")).json() == []

    async def test_update_rejects_malformed_json(self, client):
        # Mint a real token so the failure can only come from the JSON parse.
        created = await client.post("/api/mcp/tokens", json={"label": "cli", "scope": "read"})
        assert created.status == 201, await created.text()
        token_id = (await created.json())["id"]

        resp = await client.patch(
            f"/api/mcp/tokens/{token_id}",
            data=b"{{",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "Invalid JSON body"

        # The token's scope is untouched.
        listing = await (await client.get("/api/mcp/tokens")).json()
        assert [t["scope"] for t in listing] == ["read"]


# ---------------------------------------------------------------------------
# Schedules — update expiry parsing and the copy endpoint's guard rails
# ---------------------------------------------------------------------------


class TestScheduleUpdateExpiry:
    async def _make_block(self, client, room_id, **overrides):
        body = {
            "days_of_week": [0],
            "start_time": "08:00",
            "end_time": "18:00",
            "target_temp": 70.0,
        }
        body.update(overrides)
        resp = await client.post(f"/api/rooms/{room_id}/schedules", json=body)
        assert resp.status == 201, await resp.text()
        return await resp.json()

    @pytest.mark.parametrize("bad", ["not-a-datetime", "2025-13-45T99:00", 12345, ["2025-01-01"]])
    async def test_update_schedule_bad_expires_at_rejected(self, client, bad):
        room = await _create_room(client)
        block = await self._make_block(client, room["id"])
        resp = await client.put(
            f"/api/rooms/{room['id']}/schedules/{block['id']}",
            json={"expires_at": bad},
        )
        assert resp.status == 400, f"{bad!r} must be rejected"
        assert (await resp.json())["error"] == "expires_at must be a valid datetime or null"

    async def test_update_schedule_bad_expires_at_leaves_block_untouched(self, client):
        """A bad expiry must abort the whole update — the target_temp sent in the
        same body must not survive."""
        room = await _create_room(client)
        block = await self._make_block(client, room["id"], expires_at=None)
        resp = await client.put(
            f"/api/rooms/{room['id']}/schedules/{block['id']}",
            json={"target_temp": 75.0, "expires_at": "garbage"},
        )
        assert resp.status == 400
        stored = await (await client.get(f"/api/rooms/{room['id']}/schedules")).json()
        assert stored[0]["target_temp"] == 70.0
        assert stored[0]["expires_at"] is None

    async def test_update_schedule_null_expires_at_clears_it(self, client):
        """The success side of the same branch: null parses to None."""
        room = await _create_room(client)
        block = await self._make_block(client, room["id"], expires_at="2099-01-01T00:00")
        assert block["expires_at"] is not None
        resp = await client.put(
            f"/api/rooms/{room['id']}/schedules/{block['id']}",
            json={"expires_at": None},
        )
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["expires_at"] is None


class TestScheduleCopyGuards:
    async def _make_block(self, client, room_id):
        resp = await client.post(
            f"/api/rooms/{room_id}/schedules",
            json={
                "days_of_week": [0],
                "start_time": "08:00",
                "end_time": "18:00",
                "target_temp": 70.0,
            },
        )
        assert resp.status == 201, await resp.text()
        return await resp.json()

    async def test_copy_unknown_source_schedule_is_404(self, client):
        src = await _create_room(client, name="Source")
        dst = await _create_room(client, name="Dest", thermostat="climate.test")
        resp = await client.post(
            f"/api/rooms/{src['id']}/schedules/no-such-schedule/copy",
            json={"target_room_ids": [dst["id"]]},
        )
        assert resp.status == 404
        assert (await resp.json())["error"] == "Schedule not found"
        # Nothing was copied into the target room.
        assert await (await client.get(f"/api/rooms/{dst['id']}/schedules")).json() == []

    @pytest.mark.parametrize("bad_target", [123, None, {"id": "x"}])
    async def test_copy_rejects_non_string_target_ids(self, client, bad_target):
        src = await _create_room(client, name="Source")
        dst = await _create_room(client, name="Dest", thermostat="climate.test")
        block = await self._make_block(client, src["id"])
        resp = await client.post(
            f"/api/rooms/{src['id']}/schedules/{block['id']}/copy",
            json={"target_room_ids": [dst["id"], bad_target]},
        )
        assert resp.status == 400, f"{bad_target!r} must be rejected"
        assert (await resp.json())["error"] == "target_room_ids must be a list of room id strings"
        # "Validate every target up front so a bad id creates nothing": the
        # valid target listed *before* the bad one must not have been written.
        assert await (await client.get(f"/api/rooms/{dst['id']}/schedules")).json() == []

    async def test_copy_rejects_self_target(self, client):
        src = await _create_room(client, name="Source")
        block = await self._make_block(client, src["id"])
        resp = await client.post(
            f"/api/rooms/{src['id']}/schedules/{block['id']}/copy",
            json={"target_room_ids": [src["id"]]},
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "Cannot copy a schedule onto its own room"


# ---------------------------------------------------------------------------
# POST /api/thermostats — setpoint validation reached past the #213 gate
#
# ``total_vents_count`` is mandatory at registration, and its guard runs BEFORE
# every temperature check, so any of these bodies without it would 400 for the
# wrong reason and never exercise the branch under test.
# ---------------------------------------------------------------------------


BASE = {"thermostat_entity_id": "climate.gap", "total_vents_count": 6}


class TestCreateThermostatSetpointValidation:
    @pytest.mark.parametrize("field", ["min_setpoint", "max_setpoint", "default_temp"])
    async def test_non_numeric_temperature_rejected(self, client, field):
        resp = await client.post("/api/thermostats", json={**BASE, field: "bad"})
        assert resp.status == 400, f"{field}='bad' must be rejected"
        assert (await resp.json())["error"] == "Temperatures must be numeric"
        assert await (await client.get("/api/thermostats")).json() == []

    @pytest.mark.parametrize("bad", [30, 95])
    async def test_default_temp_out_of_range_rejected(self, client, bad):
        resp = await client.post("/api/thermostats", json={**BASE, "default_temp": bad})
        assert resp.status == 400
        assert (await resp.json())["error"] == "default_temp must be between 40.0 and 90.0°F"

    @pytest.mark.parametrize(
        ("field", "value"),
        [("min_setpoint", 30), ("min_setpoint", 101), ("max_setpoint", 39), ("max_setpoint", 120)],
    )
    async def test_setpoint_out_of_range_rejected(self, client, field, value):
        resp = await client.post("/api/thermostats", json={**BASE, field: value})
        assert resp.status == 400, f"{field}={value} must be rejected"
        assert (await resp.json())["error"] == "Setpoints must be between 40.0 and 100.0°F"

    async def test_min_not_less_than_max_rejected(self, client):
        resp = await client.post(
            "/api/thermostats", json={**BASE, "min_setpoint": 80, "max_setpoint": 70}
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "min_setpoint must be less than max_setpoint"

    async def test_min_equal_to_max_rejected(self, client):
        """``>=``, not ``>`` — an equal pair would leave a zero-wide band."""
        resp = await client.post(
            "/api/thermostats", json={**BASE, "min_setpoint": 70, "max_setpoint": 70}
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "min_setpoint must be less than max_setpoint"

    async def test_out_of_range_message_uses_celsius_bounds(self, client):
        """The range error is rendered in the active display unit."""
        client.app["scheduler"]._active_unit = "C"
        try:
            resp = await client.post("/api/thermostats", json={**BASE, "min_setpoint": 0})
            assert resp.status == 400
            assert (await resp.json())["error"] == "Setpoints must be between 4.4 and 37.8°C"
        finally:
            client.app["scheduler"]._active_unit = "F"


# ---------------------------------------------------------------------------
# POST /api/thermostats — per-field branches inside the write loop
# ---------------------------------------------------------------------------


class TestCreateThermostatFieldLoop:
    async def test_vacation_hvac_mode_invalid_rejected(self, client):
        resp = await client.post("/api/thermostats", json={**BASE, "vacation_hvac_mode": "auto"})
        assert resp.status == 400
        assert (await resp.json())["error"] == "vacation_hvac_mode must be 'range' or 'single'"
        assert await (await client.get("/api/thermostats")).json() == []

    @pytest.mark.parametrize("mode", ["range", "single"])
    async def test_vacation_hvac_mode_persists(self, client, mode):
        resp = await client.post("/api/thermostats", json={**BASE, "vacation_hvac_mode": mode})
        assert resp.status == 201, await resp.text()
        assert (await resp.json())["vacation_hvac_mode"] == mode
        listing = await (await client.get("/api/thermostats")).json()
        assert listing[0]["vacation_hvac_mode"] == mode

    async def test_cooling_lockout_below_f_persists(self, client):
        resp = await client.post("/api/thermostats", json={**BASE, "cooling_lockout_below_f": 55})
        assert resp.status == 201, await resp.text()
        assert (await resp.json())["cooling_lockout_below_f"] == 55.0
        listing = await (await client.get("/api/thermostats")).json()
        assert listing[0]["cooling_lockout_below_f"] == 55.0

    async def test_cooling_lockout_below_f_null_disables(self, client):
        resp = await client.post("/api/thermostats", json={**BASE, "cooling_lockout_below_f": None})
        assert resp.status == 201, await resp.text()
        assert (await resp.json())["cooling_lockout_below_f"] is None

    async def test_cooling_lockout_below_f_converts_from_celsius(self, client):
        """An absolute temperature, so 13 °C stores as 55.4 °F (with the -32)."""
        client.app["scheduler"]._active_unit = "C"
        try:
            resp = await client.post(
                "/api/thermostats", json={**BASE, "cooling_lockout_below_f": 13}
            )
            assert resp.status == 201, await resp.text()
            body = await resp.json()
        finally:
            client.app["scheduler"]._active_unit = "F"
        assert body["cooling_lockout_below_f"] == 55.4
        assert body["cooling_lockout_below_f"] != 23.4  # a delta conversion would give this

    @pytest.mark.parametrize("value", [True, False])
    async def test_overflow_during_min_runtime_persists(self, client, value):
        resp = await client.post(
            "/api/thermostats", json={**BASE, "overflow_during_min_runtime": value}
        )
        assert resp.status == 201, await resp.text()
        assert (await resp.json())["overflow_during_min_runtime"] is value
        listing = await (await client.get("/api/thermostats")).json()
        assert listing[0]["overflow_during_min_runtime"] is value

    @pytest.mark.parametrize("bad", [-1, 2.5, "10", True])
    async def test_unavailable_abort_after_min_invalid_rejected(self, client, bad):
        resp = await client.post(
            "/api/thermostats", json={**BASE, "unavailable_abort_after_min": bad}
        )
        assert resp.status == 400, f"{bad!r} must be rejected"
        assert (await resp.json())[
            "error"
        ] == "unavailable_abort_after_min must be a non-negative integer"
        assert await (await client.get("/api/thermostats")).json() == []

    @pytest.mark.parametrize("value", [0, 15])
    async def test_unavailable_abort_after_min_persists(self, client, value):
        """0 means "never abort" — it is a valid value, not a missing one."""
        resp = await client.post(
            "/api/thermostats", json={**BASE, "unavailable_abort_after_min": value}
        )
        assert resp.status == 201, await resp.text()
        assert (await resp.json())["unavailable_abort_after_min"] == value
        listing = await (await client.get("/api/thermostats")).json()
        assert listing[0]["unavailable_abort_after_min"] == value


# ---------------------------------------------------------------------------
# PUT /api/thermostats/{entity_id} — setpoint range guard
# ---------------------------------------------------------------------------


class TestUpsertThermostatSetpointRange:
    @pytest.mark.parametrize(
        ("field", "value"),
        [("min_setpoint", 30), ("min_setpoint", 101), ("max_setpoint", 39), ("max_setpoint", 120)],
    )
    async def test_setpoint_out_of_range_rejected(self, client, field, value):
        await client.post("/api/thermostats", json=BASE)
        resp = await client.put("/api/thermostats/climate.gap", json={field: value})
        assert resp.status == 400, f"PUT {field}={value} must be rejected"
        assert (await resp.json())["error"] == "Setpoints must be between 40.0 and 100.0°F"

    async def test_rejected_setpoint_leaves_stored_config_untouched(self, client):
        create = await client.post(
            "/api/thermostats", json={**BASE, "min_setpoint": 62, "max_setpoint": 78}
        )
        assert create.status == 201, await create.text()
        resp = await client.put(
            "/api/thermostats/climate.gap", json={"name": "Renamed", "min_setpoint": 30}
        )
        assert resp.status == 400
        listing = await (await client.get("/api/thermostats")).json()
        assert listing[0]["min_setpoint"] == 62.0
        assert listing[0]["name"] != "Renamed"

    async def test_boundary_values_are_accepted(self, client):
        """40 and 100 are inclusive — the guard is ``not (40 <= x <= 100)``."""
        resp = await client.put(
            "/api/thermostats/climate.bounds", json={"min_setpoint": 40, "max_setpoint": 100}
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["min_setpoint"] == 40.0
        assert body["max_setpoint"] == 100.0
