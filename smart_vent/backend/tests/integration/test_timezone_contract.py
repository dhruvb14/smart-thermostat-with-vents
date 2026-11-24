# SPDX-License-Identifier: AGPL-3.0-or-later
"""House time zone in the settings API, and the input-semantics contract (#729).

#729 moves every time the UI shows or accepts into the house's zone. It does
that entirely in the frontend, on purpose: the REST API keeps exactly the
input semantics it has today, because MCP clients and HA automations already
send these payloads. These tests pin that contract so nobody "fixes" one side
of it later, plus the new ``timezone`` / ``timezone_fallback`` settings fields.

They are also the migration guard: values written before #729 must read back
byte-identical after it.
"""

from __future__ import annotations

import os
import time as _time
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient

from backend import db
from backend.models import Room, ThermostatConfig

THERMO = "climate.main_floor"


@pytest.fixture
def house_tz(monkeypatch):
    """Run a test with the house in America/New_York, restoring UTC after."""
    monkeypatch.setenv("TZ", "America/New_York")
    if hasattr(_time, "tzset"):
        _time.tzset()
    yield
    os.environ["TZ"] = "UTC"
    if hasattr(_time, "tzset"):
        _time.tzset()


async def _seed(client: TestClient) -> None:
    conn = client.app["scheduler"]._db_conn
    await db.upsert_thermostat_config(
        conn,
        ThermostatConfig(
            thermostat_entity_id=THERMO, name="Main", min_setpoint=60, max_setpoint=80
        ),
    )
    await db.upsert_room(conn, Room(id="r1", name="Den", thermostat_entity_id=THERMO))
    await client.app["scheduler"].refresh_engines()


# ---------------------------------------------------------------------------
# GET /api/settings exposes the house's zone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_reports_configured_zone(client: TestClient, house_tz):
    data = await (await client.get("/api/settings")).json()
    assert data["timezone"] == "America/New_York"
    assert data["timezone_fallback"] is False


@pytest.mark.asyncio
async def test_settings_flags_invalid_zone_as_fallback(client: TestClient, monkeypatch):
    monkeypatch.setenv("TZ", "Not/ARealZone")
    data = await (await client.get("/api/settings")).json()
    assert data["timezone_fallback"] is True
    # The suite's process zone is UTC, so the OS fallback is a zero offset.
    assert data["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_settings_flags_unset_zone_as_fallback(client: TestClient, monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    data = await (await client.get("/api/settings")).json()
    assert data["timezone_fallback"] is True
    assert isinstance(data["timezone"], str) and data["timezone"]


# ---------------------------------------------------------------------------
# Input semantics are unchanged (the contract the frontend builds on)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vacation_offset_return_at_is_the_same_instant(client: TestClient, house_tz):
    instant = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
    # Sent with the house's offset, as the #729 frontend does.
    sent = instant.astimezone().isoformat()
    resp = await client.post("/api/settings/vacation-mode", json={"return_at": sent})
    assert resp.status == 200
    got = datetime.fromisoformat((await resp.json())["return_at"])
    assert got == instant
    await client.delete("/api/settings/vacation-mode")


@pytest.mark.asyncio
async def test_vacation_naive_return_at_is_still_utc(client: TestClient, house_tz):
    instant = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
    naive = instant.replace(tzinfo=None).isoformat()
    resp = await client.post("/api/settings/vacation-mode", json={"return_at": naive})
    assert resp.status == 200
    got = datetime.fromisoformat((await resp.json())["return_at"])
    assert got == instant, "naive return_at must keep meaning UTC (MCP/automation callers)"
    await client.delete("/api/settings/vacation-mode")


@pytest.mark.asyncio
async def test_eco_suspend_offset_and_naive_semantics(client: TestClient, house_tz):
    await _seed(client)
    instant = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    path = f"/api/thermostats/{THERMO}/eco-suspend"
    resp = await client.post(path, json={"resume_at": instant.astimezone().isoformat()})
    assert resp.status == 200
    assert datetime.fromisoformat((await resp.json())["resume_at"]) == instant
    # Naive still means UTC.
    resp = await client.post(path, json={"resume_at": instant.replace(tzinfo=None).isoformat()})
    assert resp.status == 200
    assert datetime.fromisoformat((await resp.json())["resume_at"]) == instant


@pytest.mark.asyncio
async def test_schedule_naive_expires_at_is_still_house_local(client: TestClient, house_tz):
    await _seed(client)
    expiry = (datetime.now() + timedelta(days=10)).replace(second=0, microsecond=0)
    body = {
        "days_of_week": [0, 1, 2, 3, 4, 5, 6],
        "start_time": "08:00",
        "end_time": "09:00",
        "target_temp": 70,
        "enabled": True,
        "expires_at": expiry.isoformat(timespec="minutes"),
    }
    resp = await client.post("/api/rooms/r1/schedules", json=body)
    assert resp.status in (200, 201), await resp.text()
    stored = (await resp.json())["expires_at"]
    # Naive in, naive house-local out, same wall clock: no shift by any offset.
    assert datetime.fromisoformat(stored) == expiry


# ---------------------------------------------------------------------------
# Migration guard: values written before #729 read back byte-identical
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_729_rows_read_back_unchanged(client: TestClient, house_tz):
    await _seed(client)
    conn = client.app["scheduler"]._db_conn
    scheduler = client.app["scheduler"]
    # Exactly the shapes the current release writes.
    return_at = "2031-08-14T21:00:00+00:00"
    await db.set_system_setting(conn, "vacation_mode_enabled", "1")
    await db.set_system_setting(conn, "vacation_mode_return_at", return_at)
    await db.set_eco_suspension(conn, THERMO, datetime(2031, 8, 1, 12, 30, tzinfo=UTC))
    # Simulate the upgrade: reopen the DB the way a restart/restore does.
    await scheduler.reload_db()

    settings = await (await client.get("/api/settings")).json()
    # #728 added state/start_at/welcome_home_until/missed alongside; the keys
    # that existed before read back unchanged.
    vacation = settings["vacation_mode"]
    assert {k: vacation[k] for k in ("enabled", "return_at")} == {
        "enabled": True,
        "return_at": return_at,
    }
    assert vacation["state"] == "active"
    assert vacation["start_at"] is None
    assert settings["eco_suspend"] == {THERMO: "2031-08-01T12:30:00+00:00"}
    vac = await (await client.get("/api/settings/vacation-mode")).json()
    assert vac["return_at"] == return_at
