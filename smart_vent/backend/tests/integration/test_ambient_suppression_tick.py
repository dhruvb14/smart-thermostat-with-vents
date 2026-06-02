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
