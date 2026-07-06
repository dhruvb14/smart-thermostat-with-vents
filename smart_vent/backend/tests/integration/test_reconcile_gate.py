"""The _maybe_reconcile interval gate (#433, G4).

Every existing reconcile test either calls _reconcile_state() directly or
forces the gate open (interval=1 + _last_reconciled_at=None). The gate itself
— "0 disables reconciliation entirely; otherwise fire when the interval has
elapsed" — had zero coverage, so the default-off contract could silently
invert.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"
SENSOR = "sensor.room_temp"
VENT = "cover.room_vent"


def _engine(client):
    return client.app["scheduler"]._engines[THERMO]


async def _start_running_cycle(client, fake_ha, tick) -> None:
    """Cooling cycle running for one room whose vent is open."""
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 80.0, "temperature": 78.0, "hvac_action": "cooling"},
    )
    fake_ha.seed_state(SENSOR, "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})
    resp = await client.post("/api/rooms", json={"name": "Room", "thermostat_entity_id": THERMO})
    room_id = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": SENSOR})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": VENT, "control_method": "open_close"},
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
    await tick()
    assert _engine(client).cycle_state.value == "running"


@pytest.mark.asyncio
async def test_interval_zero_disables_reconciliation(client, fake_ha, tick) -> None:
    """Default reconciliation_interval_min=0 → externally drifted vents are
    LEFT ALONE (the documented off state)."""
    await _start_running_cycle(client, fake_ha, tick)
    # Explicitly pin the default-off configuration.
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"reconciliation_interval_min": 0})
    assert resp.status == 200

    # External actor closes the active room's vent mid-cycle.
    await fake_ha.set_entity_state(VENT, "closed", {})
    _engine(client)._last_reconciled_at = None  # even a never-reconciled engine
    fake_ha.reset_calls()
    await tick()

    opens = [c for c in fake_ha.calls_for("open_cover") if c.data["entity_id"] == VENT]
    assert not opens, "interval=0 must disable corrective reconciliation entirely"
    assert fake_ha.get_state(VENT)["state"] == "closed"


@pytest.mark.asyncio
async def test_interval_gates_firing_by_elapsed_time(client, fake_ha, tick) -> None:
    """interval=5: a pass 1 minute after the last one must NOT fire; once the
    interval has elapsed the next tick fires and corrects the drift."""
    await _start_running_cycle(client, fake_ha, tick)
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"reconciliation_interval_min": 5})
    assert resp.status == 200
    eng = _engine(client)

    await fake_ha.set_entity_state(VENT, "closed", {})

    # 1 minute since the last pass — inside the interval, must not fire.
    eng._last_reconciled_at = datetime.now(UTC) - timedelta(minutes=1)
    fake_ha.reset_calls()
    await tick()
    opens = [c for c in fake_ha.calls_for("open_cover") if c.data["entity_id"] == VENT]
    assert not opens, "a pass inside the interval must not fire"

    # 6 minutes since the last pass — the gate opens and the drift is fixed.
    eng._last_reconciled_at = datetime.now(UTC) - timedelta(minutes=6)
    await tick()
    opens = [c for c in fake_ha.calls_for("open_cover") if c.data["entity_id"] == VENT]
    assert opens, "once the interval elapses the reconcile pass must correct the drift"
    assert fake_ha.get_state(VENT)["state"] == "open"
