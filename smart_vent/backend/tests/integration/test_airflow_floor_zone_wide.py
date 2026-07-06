"""Zone-wide airflow-floor accounting (Issue #421).

``required_open_vents()`` credits ``total_vents_count − <smart vents in the
supplied list>`` as always-open passive (dumb) registers. Before #421 every
call site passed a partial vent list — the active cycle's rooms only, or one
idle room at a time — so the closed smart vents of the *other* idle rooms
were miscounted as passive/open and the floor silently deflated. In an
all-smart zone, sequential idle-room closes could take the zone far below
the configured minimum with the guard reporting itself satisfied.

These tests drive full scheduler ticks against a zone where every register
is smart (6 vents, ``min_open_vents_fraction=0.5`` → 3 must stay open) and
assert the floor holds *cumulatively* across sequential close decisions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"


async def _make_room(
    client,
    name: str,
    *,
    sensor: str | None = None,
    vents: tuple[str, ...] = (),
    schedule_target: float | None = None,
) -> str:
    resp = await client.post("/api/rooms", json={"name": name, "thermostat_entity_id": THERMO})
    room_id: str = (await resp.json())["id"]
    if sensor:
        await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": sensor})
    for vent in vents:
        await client.post(
            f"/api/rooms/{room_id}/vents",
            json={"entity_id": vent, "control_method": "open_close"},
        )
    if schedule_target is not None:
        now = datetime.now(UTC)
        start = (now - timedelta(hours=1)).time().replace(second=0, microsecond=0)
        end = (now + timedelta(hours=1)).time().replace(second=0, microsecond=0)
        await client.post(
            f"/api/rooms/{room_id}/schedules",
            json={
                "days_of_week": list(range(7)),
                "start_time": start.isoformat(timespec="minutes"),
                "end_time": end.isoformat(timespec="minutes"),
                "target_temp": schedule_target,
            },
        )
    return room_id


A_VENTS = ("cover.a1", "cover.a2")
B_VENTS = ("cover.b1", "cover.b2")
C_VENTS = ("cover.c1", "cover.c2")
ALL_VENTS = A_VENTS + B_VENTS + C_VENTS


def _open_count(fake_ha) -> int:
    return sum(1 for v in ALL_VENTS if fake_ha.get_state(v)["state"] == "open")


def _room_vents_state(fake_ha, vents: tuple[str, ...]) -> set[str]:
    return {fake_ha.get_state(v)["state"] for v in vents}


def _engine(client):
    return client.app["scheduler"]._engines[THERMO]


def _seed_all_smart_zone(fake_ha) -> None:
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 80.0, "temperature": 78.0, "hvac_action": "cooling"},
    )
    for v in ALL_VENTS:
        fake_ha.seed_state(v, "open", {})


async def _configure_floor(client) -> None:
    resp = await client.put(
        f"/api/thermostats/{THERMO}",
        json={
            "total_vents_count": 6,
            "min_open_vents_fraction": 0.5,
            "has_bypass_damper": False,
        },
    )
    assert resp.status == 200


@pytest.mark.asyncio
async def test_idle_room_closes_honor_cumulative_zone_floor(client, fake_ha, tick) -> None:
    """At cycle start, sequential idle-room closes must be judged against the
    WHOLE zone: with 6 smart vents and a floor of 3, only one 2-vent idle room
    may close — the second must be deferred. Before #421 each close was judged
    against a partial pool (active + that one room), so both idle rooms closed
    and the zone ran at 2/6 open (33%) against a configured 50% floor."""
    _seed_all_smart_zone(fake_ha)
    fake_ha.seed_state("sensor.room_a", "80.0", {"unit_of_measurement": "°F"})

    await _make_room(client, "RoomA", sensor="sensor.room_a", vents=A_VENTS, schedule_target=72.0)
    await _make_room(client, "RoomB", vents=B_VENTS)
    await _make_room(client, "RoomC", vents=C_VENTS)
    await _configure_floor(client)

    await tick()  # cooling cycle starts for RoomA; idle rooms B and C evaluated

    assert _engine(client).cycle_state.value == "running"
    # Active room's vents stay open.
    assert _room_vents_state(fake_ha, A_VENTS) == {"open"}
    # Exactly one idle room closed; the other was deferred by the floor.
    b_state = _room_vents_state(fake_ha, B_VENTS)
    c_state = _room_vents_state(fake_ha, C_VENTS)
    assert {frozenset(b_state), frozenset(c_state)} == {
        frozenset({"open"}),
        frozenset({"closed"}),
    }, f"one idle room must close and one must be deferred, got B={b_state} C={c_state}"
    assert _open_count(fake_ha) == 4, "floor of 3 must hold — 4 open is the closest legal state"


@pytest.mark.asyncio
async def test_active_room_close_deferred_then_cycle_terminates_at_floor(
    client, fake_ha, tick
) -> None:
    """Mid-cycle closes are judged zone-wide too: with A+B active (4 open of 6)
    and a floor of 3, neither room may close its pair of vents. When BOTH
    rooms are at target and only the floor blocks the closes, the cycle
    terminates directly instead of running to the cycle timeout."""
    _seed_all_smart_zone(fake_ha)
    fake_ha.seed_state("sensor.room_a", "80.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state("sensor.room_b", "80.0", {"unit_of_measurement": "°F"})

    await _make_room(client, "RoomA", sensor="sensor.room_a", vents=A_VENTS, schedule_target=72.0)
    await _make_room(client, "RoomB", sensor="sensor.room_b", vents=B_VENTS, schedule_target=72.0)
    await _make_room(client, "RoomC", vents=C_VENTS)
    await _configure_floor(client)

    await tick()  # cycle starts for A+B; C (2 vents) may close: 6−2=4 ≥ 3
    assert _engine(client).cycle_state.value == "running"
    assert _room_vents_state(fake_ha, C_VENTS) == {"closed"}
    assert _open_count(fake_ha) == 4

    # RoomA reaches target — closing its 2 vents would leave 2 < 3 open, so
    # the close must be deferred and its vents stay open.
    await fake_ha.set_entity_state("sensor.room_a", "72.0", {"unit_of_measurement": "°F"})
    await tick()
    assert _engine(client).cycle_state.value == "running"
    assert _room_vents_state(fake_ha, A_VENTS) == {"open"}, (
        "close must be deferred — the zone-wide floor forbids dropping to 2 open"
    )

    # RoomB reaches target too. Every room is now at target and only the
    # airflow floor blocks the remaining closes → the cycle terminates
    # (the open vents already ARE the idle state) rather than idling at
    # temperature until the cycle timeout.
    await fake_ha.set_entity_state("sensor.room_b", "72.0", {"unit_of_measurement": "°F"})
    await tick()
    assert _engine(client).cycle_state.value == "idle", (
        "cycle must terminate when only floor-deferred closes block it"
    )
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["ended_at"] is not None
    # Termination re-opens the whole zone.
    assert _open_count(fake_ha) == 6
