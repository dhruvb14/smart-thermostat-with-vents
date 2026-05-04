"""
Regression test: idle-room vents must be re-closed after a container reboot
that resumes a running cycle via ``restore_from_db``.

Scenario (the original bug report):
  - Room A is active via schedule; Room B is idle on the same thermostat zone.
  - A cycle starts → Room B's vent is closed by ``_start_or_update_cycle``.
  - The container reboots mid-cycle.  The open cycle log is in DB, but
    something external (an HA automation, a manual override, or HA itself
    reloading with defaults) has re-opened Room B's vent.
  - On startup, the scheduler creates a fresh ``CycleEngine`` and calls
    ``restore_from_db``.  Before the fix, restore only rebuilt in-memory
    state and left idle vents open until the cycle naturally ended.  After
    the fix, restore re-asserts idle-vent closure so the restored cycle
    behaves identically to the pre-restart one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.engine.cycle_engine import CycleEngine, CycleState


@pytest.mark.asyncio
async def test_restore_closes_idle_room_vents(client, fake_ha, tick) -> None:
    thermostat = "climate.test_thermostat"
    sensor_a = "sensor.room_a_temp"
    vent_a = "cover.room_a_vent"
    sensor_b = "sensor.room_b_temp"
    vent_b = "cover.room_b_vent"

    # Cooling scenario: ambient warm, both rooms start with vents open
    # (the state _terminate_cycle leaves the zone in between cycles).
    fake_ha.seed_state(
        thermostat,
        "cool",
        {"current_temperature": 76.0, "temperature": 76.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state(sensor_a, "78.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(vent_a, "open", {})
    fake_ha.seed_state(sensor_b, "73.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(vent_b, "open", {})

    # Room A: has a schedule active now → will drive the cycle.
    resp = await client.post(
        "/api/rooms",
        json={"name": "Room A", "thermostat_entity_id": thermostat},
    )
    room_a_id = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_a_id}/sensors", json={"entity_id": sensor_a})
    await client.post(
        f"/api/rooms/{room_a_id}/vents",
        json={"entity_id": vent_a, "control_method": "open_close"},
    )
    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).time().replace(second=0, microsecond=0)
    end = (now + timedelta(hours=1)).time().replace(second=0, microsecond=0)
    await client.post(
        f"/api/rooms/{room_a_id}/schedules",
        json={
            "days_of_week": list(range(7)),
            "start_time": start.isoformat(timespec="minutes"),
            "end_time": end.isoformat(timespec="minutes"),
            "target_temp": 72.0,
        },
    )

    # Room B: idle on the same zone (no schedule, no presence).
    resp = await client.post(
        "/api/rooms",
        json={"name": "Room B", "thermostat_entity_id": thermostat},
    )
    room_b_id = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_b_id}/sensors", json={"entity_id": sensor_b})
    await client.post(
        f"/api/rooms/{room_b_id}/vents",
        json={"entity_id": vent_b, "control_method": "open_close"},
    )

    # Start a cycle.  The existing fresh-start sweep closes vent B.
    await tick()
    assert fake_ha.get_state(vent_b)["state"] == "closed", (
        "precondition: fresh-start sweep should have closed idle room B's vent"
    )

    # Sanity: exactly one open cycle log in DB.
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1
    assert logs[0]["ended_at"] is None

    # ------------------------------------------------------------------
    # Simulate a container reboot mid-cycle.
    #
    # In real life the whole process dies, then a new one starts.  The DB
    # still has the open cycle log; HA state may have drifted.  For the
    # regression we reproduce the exact failure condition: vent B is open
    # again at the moment ``restore_from_db`` runs.
    # ------------------------------------------------------------------
    await fake_ha.set_entity_state(vent_b, "open", {})
    fake_ha.reset_calls()

    scheduler = client.app["scheduler"]
    new_engine = CycleEngine(
        thermostat_entity_id=thermostat,
        ha=fake_ha,
        vent_ctrl=scheduler._vent_ctrl,
        broadcast=None,
        event_logger=scheduler._event_logger,
        get_enabled=lambda: True,
    )
    await new_engine.restore_from_db(scheduler._db_conn)

    # Engine should be RUNNING (cycle was restored, not discarded).
    assert new_engine.cycle_state == CycleState.RUNNING, (
        f"restored cycle should be RUNNING; state={new_engine.cycle_state}"
    )

    # The key assertion: vent B must have been closed as part of restore.
    close_calls = fake_ha.calls_for("close_cover")
    closed_entities = {c.data["entity_id"] for c in close_calls}
    assert vent_b in closed_entities, (
        f"restore_from_db must close idle room B's vent; "
        f"close calls: {close_calls}, all calls: {fake_ha.calls}"
    )
    assert vent_a not in closed_entities, "active room A vent must not be closed on restore"
    assert fake_ha.get_state(vent_b)["state"] == "closed", (
        "vent B state should reflect the close_cover call"
    )
