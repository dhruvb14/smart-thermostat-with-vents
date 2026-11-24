# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scheduled vacation and welcome home over REST and MQTT (Issue #728).

The scheduler-level state machine is in ``tests/test_scheduled_vacation.py``;
this file pins the wire contract: request validation, the 409, the additive
response fields, the welcome-home thermostat fields, and the MQTT topics.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient

from backend import db
from backend.models import Room, ThermostatConfig

from .test_mqtt_bridge import PREFIX, bridge  # noqa: F401  (fixture re-export)

THERMO = "climate.upstairs"
PATH = "/api/settings/vacation-mode"


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).replace(microsecond=0).isoformat()


async def _seed(client: TestClient) -> None:
    conn = client.app["scheduler"]._db_conn
    await db.upsert_thermostat_config(
        conn,
        ThermostatConfig(thermostat_entity_id=THERMO, name="Up", min_setpoint=62, max_setpoint=80),
    )
    await db.upsert_room(conn, Room(id="r1", name="Den", thermostat_entity_id=THERMO))
    await client.app["scheduler"].refresh_engines()


# ---------------------------------------------------------------------------
# POST / GET / DELETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_without_start_is_byte_compatible(client: TestClient):
    ret = _iso(timedelta(days=3))
    resp = await client.post(PATH, json={"return_at": ret})
    assert resp.status == 200
    body = await resp.json()
    assert body["enabled"] is True and body["return_at"] == ret
    assert body["state"] == "active" and body["start_at"] is None
    await client.delete(PATH)


@pytest.mark.asyncio
async def test_post_future_start_schedules_and_get_reports_it(client: TestClient):
    start, ret = _iso(timedelta(days=1)), _iso(timedelta(days=4))
    resp = await client.post(PATH, json={"start_at": start, "return_at": ret})
    assert resp.status == 200
    body = await resp.json()
    assert body == {
        "enabled": False,
        "state": "scheduled",
        "start_at": start,
        "return_at": ret,
        "welcome_home_until": None,
        "missed": None,
    }
    assert (await (await client.get(PATH)).json()) == body
    settings = await (await client.get("/api/settings")).json()
    assert settings["vacation_mode"]["state"] == "scheduled"

    # DELETE cancels the scheduled trip.
    resp = await client.delete(PATH)
    assert (await resp.json())["state"] == "off"


@pytest.mark.asyncio
async def test_naive_start_means_utc(client: TestClient):
    start = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0, tzinfo=None)
    resp = await client.post(
        PATH, json={"start_at": start.isoformat(), "return_at": _iso(timedelta(days=4))}
    )
    body = await resp.json()
    assert datetime.fromisoformat(body["start_at"]) == start.replace(tzinfo=UTC)
    await client.delete(PATH)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, message",
    [
        ({}, "return_at (ISO-8601 UTC datetime) is required"),
        ({"return_at": "garbage"}, "return_at must be a valid ISO-8601 datetime"),
        ({"return_at": 12}, "return_at must be a valid ISO-8601 datetime"),
        ({"return_at": "2001-01-01T00:00:00Z"}, "return_at must be in the future"),
        (
            {"return_at": "2099-01-02T00:00:00Z", "start_at": "nope"},
            "start_at must be a valid ISO-8601 datetime",
        ),
        (
            {"return_at": "2099-01-02T00:00:00Z", "start_at": "2099-01-02T00:00:00Z"},
            "start_at must be before return_at",
        ),
    ],
)
async def test_post_validation(client: TestClient, payload, message):
    resp = await client.post(PATH, json=payload)
    assert resp.status == 400
    assert (await resp.json())["error"] == message


@pytest.mark.asyncio
async def test_post_non_object_body_is_rejected(client: TestClient):
    resp = await client.post(PATH, json=["x"])
    assert resp.status == 400


@pytest.mark.asyncio
async def test_future_start_while_active_is_409(client: TestClient):
    await client.post(PATH, json={"return_at": _iso(timedelta(days=3))})
    resp = await client.post(
        PATH, json={"start_at": _iso(timedelta(days=1)), "return_at": _iso(timedelta(days=4))}
    )
    assert resp.status == 409
    # The exact fixed message: exception text never reaches the body (alert #4).
    assert (await resp.json())["error"] == (
        "vacation mode is already active, so its start cannot be changed"
    )
    await client.delete(PATH)


@pytest.mark.asyncio
async def test_delete_welcome_home_flag(client: TestClient):
    await _seed(client)
    await client.post(PATH, json={"return_at": _iso(timedelta(days=3))})
    resp = await client.delete(f"{PATH}?welcome_home=false")
    assert resp.status == 200
    assert client.app["scheduler"]._welcome_fired == {}

    await client.post(PATH, json={"return_at": _iso(timedelta(days=3))})
    resp = await client.delete(PATH)
    assert set(client.app["scheduler"]._welcome_fired) == {THERMO}
    assert (await resp.json())["welcome_home_until"] is not None

    resp = await client.delete(f"{PATH}?welcome_home=maybe")
    assert resp.status == 400


@pytest.mark.asyncio
async def test_missed_notice_round_trip(client: TestClient):
    scheduler = client.app["scheduler"]
    start = datetime.now(UTC) + timedelta(days=1)
    ret = start + timedelta(days=1)
    await scheduler.plan_vacation(ret, start)
    await scheduler._check_vacation_start(now=ret + timedelta(hours=1))
    body = await (await client.get(PATH)).json()
    assert body["state"] == "off"
    assert body["missed"] == {"start_at": start.isoformat(), "return_at": ret.isoformat()}

    resp = await client.delete(f"{PATH}/missed-notice")
    assert resp.status == 200
    assert (await resp.json())["missed"] is None


# ---------------------------------------------------------------------------
# Welcome-home thermostat fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_welcome_home_fields_round_trip(client: TestClient):
    await _seed(client)
    resp = await client.put(
        f"/api/thermostats/{THERMO}",
        json={
            "welcome_home_enabled": False,
            "welcome_home_lead_min": 90,
            "welcome_home_hours": 2.5,
        },
    )
    assert resp.status == 200, await resp.text()
    rows = await (await client.get("/api/thermostats")).json()
    row = next(r for r in rows if r["thermostat_entity_id"] == THERMO)
    assert (
        row["welcome_home_enabled"],
        row["welcome_home_lead_min"],
        row["welcome_home_hours"],
    ) == (
        False,
        90,
        2.5,
    )


@pytest.mark.asyncio
async def test_welcome_home_fields_on_create(client: TestClient):
    resp = await client.post(
        "/api/thermostats",
        json={
            "thermostat_entity_id": "climate.new",
            "total_vents_count": 4,
            "welcome_home_lead_min": 30,
            "welcome_home_hours": 0.25,
        },
    )
    assert resp.status in (200, 201), await resp.text()
    body = await resp.json()
    assert body["welcome_home_lead_min"] == 30 and body["welcome_home_hours"] == 0.25
    assert body["welcome_home_enabled"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field, value, message",
    [
        ("welcome_home_enabled", "yes", "welcome_home_enabled must be true or false"),
        ("welcome_home_lead_min", "60", "welcome_home_lead_min must be a number"),
        ("welcome_home_lead_min", True, "welcome_home_lead_min must be a number"),
        ("welcome_home_lead_min", 12.5, "welcome_home_lead_min must be a whole number of minutes"),
        ("welcome_home_lead_min", 241, "welcome_home_lead_min must be between 0 and 240"),
        ("welcome_home_lead_min", -1, "welcome_home_lead_min must be between 0 and 240"),
        ("welcome_home_hours", 0.1, "welcome_home_hours must be between 0.25 and 12"),
        ("welcome_home_hours", 13, "welcome_home_hours must be between 0.25 and 12"),
    ],
)
async def test_welcome_home_validation(client: TestClient, field, value, message):
    await _seed(client)
    resp = await client.put(f"/api/thermostats/{THERMO}", json={field: value})
    assert resp.status == 400
    assert (await resp.json())["error"] == message


@pytest.mark.asyncio
async def test_welcome_home_validation_on_create(client: TestClient):
    resp = await client.post(
        "/api/thermostats",
        json={
            "thermostat_entity_id": "climate.new",
            "total_vents_count": 4,
            "welcome_home_hours": 0,
        },
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_lead_minutes_accept_a_whole_float(client: TestClient):
    await _seed(client)
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"welcome_home_lead_min": 45.0})
    assert resp.status == 200
    tc = await db.get_thermostat_config(client.app["scheduler"]._db_conn, THERMO)
    assert tc.welcome_home_lead_min == 45 and isinstance(tc.welcome_home_lead_min, int)


# ---------------------------------------------------------------------------
# MQTT (§7)
# ---------------------------------------------------------------------------


def _result(bridge, key: str) -> dict:  # noqa: F811
    result: dict = bridge.transport.results()[f"{PREFIX}/system/{key}/set/result"]
    return result


@pytest.mark.asyncio
async def test_mqtt_schedule_topic_creates_a_trip(client, bridge, fake_ha) -> None:  # noqa: F811
    payload = json.dumps(
        {"start_at": "2099-03-01T08:00:00-05:00", "return_at": "2099-03-08T20:00:00-05:00"}
    )
    await bridge.handle_message(f"{PREFIX}/system/vacation_mode/schedule/set", payload)
    assert _result(bridge, "vacation_mode/schedule")["ok"] is True
    body = await (await client.get(PATH)).json()
    assert body["state"] == "scheduled"
    assert body["start_at"] == "2099-03-01T13:00:00+00:00"

    await bridge.sync()
    retained = bridge.transport.retained()
    assert retained[f"{PREFIX}/system/vacation_state/state"] == "scheduled"
    assert retained[f"{PREFIX}/system/vacation_mode/start_at/state"] == "2099-03-01T13:00:00+00:00"
    assert retained[f"{PREFIX}/system/vacation_mode/state"] == "OFF"
    # The schedule topic is command-only: no retained state, no HA entity.
    assert f"{PREFIX}/system/vacation_mode/schedule/state" not in retained

    # Editing the start keeps the return; editing the return keeps the start.
    await bridge.handle_message(
        f"{PREFIX}/system/vacation_mode/start_at/set", "2099-03-02T08:00:00-05:00"
    )
    assert _result(bridge, "vacation_mode/start_at")["ok"] is True
    await bridge.handle_message(
        f"{PREFIX}/system/vacation_mode/return_at/set", "2099-03-09T20:00:00-05:00"
    )
    body = await (await client.get(PATH)).json()
    assert body["state"] == "scheduled"
    assert body["start_at"] == "2099-03-02T13:00:00+00:00"
    assert body["return_at"] == "2099-03-10T01:00:00+00:00"

    # The switch's OFF cancels a scheduled trip too.
    await bridge.handle_message(f"{PREFIX}/system/vacation_mode/set", "OFF")
    assert (await (await client.get(PATH)).json())["state"] == "off"


@pytest.mark.asyncio
async def test_mqtt_start_at_needs_a_scheduled_trip(client, bridge, fake_ha) -> None:  # noqa: F811
    await bridge.handle_message(
        f"{PREFIX}/system/vacation_mode/start_at/set", "2099-03-02T08:00:00-05:00"
    )
    result = _result(bridge, "vacation_mode/start_at")
    assert result["ok"] is False and "no vacation is scheduled" in result["error"]

    await client.post(PATH, json={"return_at": _iso(timedelta(days=3))})
    await bridge.handle_message(
        f"{PREFIX}/system/vacation_mode/start_at/set", "2099-03-02T08:00:00-05:00"
    )
    result = _result(bridge, "vacation_mode/start_at")
    assert result["ok"] is False and "already active" in result["error"]
    await bridge.sync()
    assert bridge.transport.retained()[f"{PREFIX}/system/vacation_state/state"] == "active"
    await client.delete(PATH)


@pytest.mark.asyncio
async def test_mqtt_bad_schedule_and_read_only_state(client, bridge, fake_ha) -> None:  # noqa: F811
    await bridge.handle_message(f"{PREFIX}/system/vacation_mode/schedule/set", "{nope")
    assert _result(bridge, "vacation_mode/schedule")["ok"] is False
    await bridge.handle_message(f"{PREFIX}/system/vacation_state/set", "active")
    result = _result(bridge, "vacation_state")
    assert result["ok"] is False and "read-only" in result["error"]
