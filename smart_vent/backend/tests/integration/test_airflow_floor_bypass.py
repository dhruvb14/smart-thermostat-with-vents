"""Airflow-floor bypass & whole-zone enforcement tests (Issue #210).

Three protections cooperate to keep duct static pressure safe, and they
interact subtly enough that the issue called for explicit coverage:

  1. ``_monitor_rooms`` has a deliberate **last-vent bypass** — when the last
     room needing to close would drop open vents below the airflow floor, the
     engine closes it anyway because the cycle is about to terminate and the
     terminate-reopen brings every zone vent back open.
  2. ``_close_idle_room_vents`` and the ``_start_or_update_cycle`` removed-room
     loop must respect the airflow floor across the **whole zone** (active +
     idle), not just the rooms in the cycle.
  3. After the bypass, ``_terminate_cycle`` must immediately reopen all zone
     vents in the same tick — otherwise an all-vents-closed window with the
     blower coasting on thermal lag would dead-head the system.

Issue #213 consolidated the per-tick floor calculation into the shared
``required_open_vents`` helper.  This file pins the engine-side invariants
those callsites still owe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _all_day_schedule(client, room_id: str, target: float) -> str:
    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).time().replace(second=0, microsecond=0)
    end = (now + timedelta(hours=1)).time().replace(second=0, microsecond=0)
    resp = await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={
            "days_of_week": list(range(7)),
            "start_time": start.isoformat(timespec="minutes"),
            "end_time": end.isoformat(timespec="minutes"),
            "target_temp": target,
        },
    )
    sched_id: str = (await resp.json())["id"]
    return sched_id


async def _heating_room(
    client,
    fake_ha,
    name: str,
    sensor: str,
    vent: str,
    target: float | None,
    room_temp: float,
) -> str:
    """Create a room with the given sensor + vent; schedule it if ``target`` set."""
    fake_ha.seed_state(sensor, str(room_temp), {"unit_of_measurement": "°F"})
    fake_ha.seed_state(vent, "open", {})
    resp = await client.post("/api/rooms", json={"name": name, "thermostat_entity_id": THERMO})
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": sensor})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": vent, "control_method": "open_close"},
    )
    if target is not None:
        await _all_day_schedule(client, room_id, target)
    return room_id


def _seed_heating_thermostat(fake_ha, ambient: float = 65.0) -> None:
    fake_ha.seed_state(
        THERMO,
        "heat",
        {"current_temperature": ambient, "temperature": ambient, "hvac_action": "idle"},
    )


async def _warning_messages(client) -> list[str]:
    events = await (await client.get("/api/logs/events?level=warning")).json()
    return [e["message"] for e in events]


async def _register_thermostat(client, **fields) -> None:
    """Register THERMO with the given airflow config; total_vents_count required (#213)."""
    body = {"thermostat_entity_id": THERMO, **fields}
    body.setdefault("total_vents_count", 2)
    resp = await client.post("/api/thermostats", json=body)
    assert resp.status == 201, await resp.json()


# ===========================================================================
# Last-vent bypass — invariants the issue called out
# ===========================================================================


@pytest.mark.asyncio
async def test_last_vent_bypass_close_is_paired_with_immediate_reopen(
    client, fake_ha, tick
) -> None:
    """The whole point of #210's concern: when the last vent is closed via the
    airflow-floor bypass, the cycle terminates in the same tick and
    ``_terminate_cycle`` re-opens all zone vents.  The vent must end up *open*,
    even though it was momentarily closed."""
    _seed_heating_thermostat(fake_ha)
    # Floor = ceil(1 * 1.0) = 1 → with one smart vent, the close would be
    # blocked except for the last-vent bypass.
    await _register_thermostat(client, total_vents_count=1, min_open_vents_fraction=1.0)
    await _heating_room(
        client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", target=68.0, room_temp=60.0
    )

    await tick()  # cycle starts; vent already open
    assert len(await (await client.get("/api/logs")).json()) == 1

    # Room reaches target — the engine wants to close its only vent. The
    # airflow floor would block the close, but the last-vent bypass kicks in
    # and the close happens anyway, immediately followed by the terminate
    # re-opening all vents.
    await fake_ha.set_entity_state("sensor.bed", "68.0", {"unit_of_measurement": "°F"})
    await tick()

    # close_cover *was* called (the bypass) AND open_cover *was* called (the
    # terminate reopen).
    close_calls = fake_ha.calls_for("close_cover")
    open_calls = fake_ha.calls_for("open_cover")
    assert any(c.data.get("entity_id") == "cover.bed" for c in close_calls), close_calls
    assert any(c.data.get("entity_id") == "cover.bed" for c in open_calls), open_calls

    # Final state: the vent is open. The bypass window must not be observable
    # after the tick.
    assert fake_ha.get_state("cover.bed")["state"] == "open"

    # The cycle ended cleanly.
    closed = (await (await client.get("/api/logs")).json())[0]
    assert closed["ended_at"] is not None
    assert closed["ended_reason"] == "completed"


@pytest.mark.asyncio
async def test_last_vent_bypass_emits_a_warning_so_it_is_auditable(client, fake_ha, tick) -> None:
    """The bypass is a *deliberate* safety relaxation, so it must surface in
    the event log — a technician reading the cycle's events should be able to
    see it happened."""
    _seed_heating_thermostat(fake_ha)
    await _register_thermostat(client, total_vents_count=1, min_open_vents_fraction=1.0)
    await _heating_room(
        client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", target=68.0, room_temp=60.0
    )
    await tick()
    await fake_ha.set_entity_state("sensor.bed", "68.0", {"unit_of_measurement": "°F"})
    await tick()

    warnings = await _warning_messages(client)
    assert any("bypassing the airflow floor" in m for m in warnings), warnings


# ===========================================================================
# Whole-zone airflow floor — _close_idle_room_vents must look at active + idle
# ===========================================================================


@pytest.mark.asyncio
async def test_idle_room_close_respects_floor_across_active_and_idle(client, fake_ha, tick) -> None:
    """Two smart vents on the same thermostat. With total=2 and fraction=1.0
    the floor is 2 open — there's no room to close anything. The idle room's
    vent must not be closed at cycle start, because doing so would drop the
    zone-wide open count below the floor."""
    _seed_heating_thermostat(fake_ha)
    # total=2 smart, fraction=1.0 → required=2 → no closes permitted while
    # the cycle runs (the last-vent bypass only fires at terminate).
    await _register_thermostat(client, total_vents_count=2, min_open_vents_fraction=1.0)

    # Active room: cold, schedule active → joins the cycle.
    await _heating_room(
        client, fake_ha, "Active", "sensor.active", "cover.active", target=68.0, room_temp=60.0
    )
    # Idle room: no schedule, so it never joins. Its vent should be closed by
    # _close_idle_room_vents at cycle start — but the floor forbids it.
    await _heating_room(
        client, fake_ha, "Idle", "sensor.idle", "cover.idle", target=None, room_temp=72.0
    )

    await tick()

    # The active room's vent is open; the idle room's vent stays open because
    # closing it would violate the floor.
    assert fake_ha.get_state("cover.active")["state"] == "open"
    assert fake_ha.get_state("cover.idle")["state"] == "open", (
        "idle vent must stay open — closing it would drop zone-wide below the floor"
    )
    # Nothing was issued to the idle vent at all.
    close_calls = fake_ha.calls_for("close_cover")
    assert not any(c.data.get("entity_id") == "cover.idle" for c in close_calls), close_calls

    # The deferral is logged for the operator — both in the engine's python
    # log (technician console) and as a warning event in the UI Live Feed.
    warnings = await _warning_messages(client)
    assert any("airflow floor" in m for m in warnings), warnings


@pytest.mark.asyncio
async def test_idle_room_close_proceeds_when_passive_vents_satisfy_the_floor(
    client, fake_ha, tick
) -> None:
    """Mirror of the above: total_vents_count=12 with 2 smart vents leaves 10
    passive registers always open. The airflow floor is satisfied by the
    passive vents alone, so the idle-room close is allowed."""
    _seed_heating_thermostat(fake_ha)
    # ceil(12 * 1/3) - (12 - 2) = 4 - 10 → clamped to 0. Idle close is fine.
    await _register_thermostat(client, total_vents_count=12, min_open_vents_fraction=1.0 / 3.0)

    await _heating_room(
        client, fake_ha, "Active", "sensor.active", "cover.active", target=68.0, room_temp=60.0
    )
    await _heating_room(
        client, fake_ha, "Idle", "sensor.idle", "cover.idle", target=None, room_temp=72.0
    )

    await tick()

    assert fake_ha.get_state("cover.active")["state"] == "open"
    assert fake_ha.get_state("cover.idle")["state"] == "closed", (
        "idle vent should close when passive vents already satisfy the floor"
    )


# ===========================================================================
# Stability — the brief bypass window must not produce phantom activity on
# the next tick (the "reconciler does not fight the engine" guard)
# ===========================================================================


@pytest.mark.asyncio
async def test_post_bypass_tick_is_quiet(client, fake_ha, tick) -> None:
    """Once the bypass-close + terminate-reopen sequence has run, the next
    tick must not produce any new vent or setpoint commands. This guards
    against the reconciler (or any other late observer) seeing the brief
    closed-vent state and fighting the engine."""
    _seed_heating_thermostat(fake_ha)
    await _register_thermostat(
        client,
        total_vents_count=1,
        min_open_vents_fraction=1.0,
        reconciliation_interval_min=1,
    )
    await _heating_room(
        client, fake_ha, "Bedroom", "sensor.bed", "cover.bed", target=68.0, room_temp=60.0
    )

    await tick()
    await fake_ha.set_entity_state("sensor.bed", "68.0", {"unit_of_measurement": "°F"})
    await tick()  # bypass-close + terminate-reopen

    # Reset the call recorder, then tick again. The engine is IDLE; the
    # reconciler runs but finds nothing to do.
    fake_ha.reset_calls()
    # Force a reconcile on the next tick by clearing the last-run timestamp.
    engine = client.app["scheduler"]._engines[THERMO]
    engine._last_reconciled_at = None

    await tick()

    # No further vent commands — the bypass closed-vent state must not be
    # observed by anything that would try to re-open or re-close it.  Setpoint
    # writes are allowed: the engine idempotently re-asserts ambient while
    # IDLE, which keeps the thermostat aligned but does not fight the bypass.
    assert fake_ha.calls_for("close_cover") == [], fake_ha.calls_for("close_cover")
    assert fake_ha.calls_for("open_cover") == [], fake_ha.calls_for("open_cover")
    # And the engine is still settled.
    assert engine.cycle_state.value == "idle"
    assert fake_ha.get_state("cover.bed")["state"] == "open"
