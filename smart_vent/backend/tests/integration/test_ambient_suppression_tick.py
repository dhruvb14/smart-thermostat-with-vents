"""Full-tick integration tests for ambient pre-cool/pre-heat (Issue #248).

The unit tests in test_cycle_engine.py exercise the per-room vote in isolation;
these drive the whole scheduler tick against a fake Home Assistant to prove the
wiring (outside-temp read → mode inference → cycle decision) actually suppresses
a real presence-driven cycle, and that the control case still runs HVAC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend import db as _db
from backend.models import PresenceHoldoverState

THERMO = "climate.test_thermostat"
ROOM_SENSOR = "sensor.office_temp"
ROOM_VENT = "cover.office_vent"
OUTDOOR = "sensor.outdoor"


def _seed_cold_room(fake_ha, outside_f: str) -> None:
    """Room at 67°F wanting 70°F (would normally heat); thermostat in heat mode."""
    fake_ha.seed_state(
        THERMO,
        "heat",
        {"current_temperature": 67.0, "temperature": 70.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state(ROOM_SENSOR, "67.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(ROOM_VENT, "open", {})
    fake_ha.seed_state(OUTDOOR, outside_f, {"unit_of_measurement": "°F"})


async def _create_presence_room(client, **ambient):
    """Create a presence-active room (planted holdover) with the given ambient config."""
    resp = await client.post(
        "/api/rooms",
        json={
            "name": "Office",
            "thermostat_entity_id": THERMO,
            "system_wide_temp": 70.0,
            "presence_holdover_hours": 2.0,
            **ambient,
        },
    )
    assert resp.status == 201, await resp.text()
    room_id = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": ROOM_SENSOR})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": ROOM_VENT, "control_method": "open_close"},
    )
    # Plant an active presence holdover so the room resolves as source="presence".
    conn = client.app["scheduler"]._db_conn
    now = datetime.now(UTC)
    await _db.upsert_holdover_state(
        conn,
        PresenceHoldoverState(
            room_id=room_id, last_detected_at=now, expires_at=now + timedelta(hours=2)
        ),
    )
    return room_id


@pytest.mark.asyncio
async def test_presence_heat_suppressed_when_outside_warm(client, fake_ha, tick) -> None:
    """Warm outside + feature on → the presence heating demand is suppressed and
    no cycle starts (the room coasts; vents rest)."""
    _seed_cold_room(fake_ha, outside_f="80.0")  # 80 >= 70 + 5 → coast up
    await _create_presence_room(
        client,
        ambient_suppression_enabled=True,
        ambient_suppression_mode="any_presence",
        ambient_suppression_min_differential=5,
        ambient_suppression_deadband=3,
    )
    await client.put("/api/settings/outside-temp-entity", json={"entity_id": OUTDOOR})

    await tick()

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 0, "no cycle should start while the room is coasting"
    assert client.app["scheduler"]._engines[THERMO].cycle_state.value == "idle"


@pytest.mark.asyncio
async def test_presence_heat_runs_when_differential_too_small(client, fake_ha, tick) -> None:
    """Only +1°F outside (< 5 differential) → no coasting → a heating cycle runs.

    Identical to the suppression case except the outside reading, proving the
    feature — not some other idleness — is what held the cycle off."""
    _seed_cold_room(fake_ha, outside_f="71.0")  # 71 < 70 + 5 → gate not met
    await _create_presence_room(
        client,
        ambient_suppression_enabled=True,
        ambient_suppression_mode="any_presence",
        ambient_suppression_min_differential=5,
        ambient_suppression_deadband=3,
    )
    await client.put("/api/settings/outside-temp-entity", json={"entity_id": OUTDOOR})

    await tick()

    assert client.app["scheduler"]._engines[THERMO].cycle_state.value == "running"
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) >= 1, "a heating cycle should start when coasting is not warranted"


@pytest.mark.asyncio
async def test_coasting_room_vents_closed_while_another_room_cycles(client, fake_ha, tick) -> None:
    """#416: pins the two-room behavior the docs now describe — a coasting
    (suppressed) room is excluded from another room's cycle AND its vents are
    closed for that cycle's duration, like any idle room's, so the active
    cycle's supply air cannot fight the coast."""
    _seed_cold_room(fake_ha, outside_f="80.0")  # Office coasts up (80 ≥ 70 + 5)
    await _create_presence_room(
        client,
        ambient_suppression_enabled=True,
        ambient_suppression_mode="any_presence",
        ambient_suppression_min_differential=5,
        ambient_suppression_deadband=3,
    )
    await client.put("/api/settings/outside-temp-entity", json={"entity_id": OUTDOOR})

    # Second room with a schedule demanding heat drives a real cycle.
    fake_ha.seed_state("sensor.den_temp", "65.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.den_vent", "open", {})
    resp = await client.post("/api/rooms", json={"name": "Den", "thermostat_entity_id": THERMO})
    den_id = (await resp.json())["id"]
    await client.post(f"/api/rooms/{den_id}/sensors", json={"entity_id": "sensor.den_temp"})
    await client.post(
        f"/api/rooms/{den_id}/vents",
        json={"entity_id": "cover.den_vent", "control_method": "open_close"},
    )
    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).time().replace(second=0, microsecond=0)
    end = (now + timedelta(hours=1)).time().replace(second=0, microsecond=0)
    await client.post(
        f"/api/rooms/{den_id}/schedules",
        json={
            "days_of_week": list(range(7)),
            "start_time": start.isoformat(timespec="minutes"),
            "end_time": end.isoformat(timespec="minutes"),
            "target_temp": 70.0,
        },
    )

    await tick()

    # The Den's heating cycle runs; the coasting Office is not in it.
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["mode"] == "heating"
    detail = await (await client.get(f"/api/logs/{logs[0]['id']}/detail")).json()
    assert [r["name"] for r in detail["rooms"]] == ["Den"]
    # And the coasting room's vent is CLOSED for the cycle, like any idle room.
    assert fake_ha.get_state(ROOM_VENT)["state"] == "closed", (
        "the coasting room's vent must be closed while another room's cycle runs"
    )
    assert fake_ha.get_state("cover.den_vent")["state"] == "open"
