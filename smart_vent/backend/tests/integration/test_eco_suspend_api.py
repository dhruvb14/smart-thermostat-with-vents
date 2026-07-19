"""Eco Suspend API + engine integration tests (Issue #500).

POST   /api/thermostats/{id}/eco-suspend → suspend until resume_at
DELETE /api/thermostats/{id}/eco-suspend → cancel early

Also drives the full engine against the fake HA to verify the zone-wide
suspension gate and the next-cycle-only semantics end-to-end, and pins the
route-order regression that used to shadow suffixed DELETEs under
/api/thermostats/{entity_id}.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

THERMO = "climate.test_thermostat"
OUTDOOR = "sensor.outdoor_temp"


def _future_iso(hours: float = 6.0) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


async def _configure_thermostat(client, **extra) -> None:
    resp = await client.put(f"/api/thermostats/{THERMO}", json={"name": "Test", **extra})
    assert resp.status == 200


# ---------------------------------------------------------------------------
# Endpoint validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suspend_unknown_thermostat_404(client) -> None:
    resp = await client.post(
        "/api/thermostats/climate.nope/eco-suspend", json={"resume_at": _future_iso()}
    )
    assert resp.status == 404


@pytest.mark.asyncio
async def test_suspend_requires_resume_at(client) -> None:
    await _configure_thermostat(client)
    resp = await client.post(f"/api/thermostats/{THERMO}/eco-suspend", json={})
    assert resp.status == 400
    assert "resume_at" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_suspend_rejects_malformed_resume_at(client) -> None:
    await _configure_thermostat(client)
    resp = await client.post(
        f"/api/thermostats/{THERMO}/eco-suspend", json={"resume_at": "not-a-date"}
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_suspend_rejects_past_resume_at(client) -> None:
    await _configure_thermostat(client)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    resp = await client.post(f"/api/thermostats/{THERMO}/eco-suspend", json={"resume_at": past})
    assert resp.status == 400
    assert "future" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_suspend_naive_datetime_treated_as_utc(client) -> None:
    await _configure_thermostat(client)
    naive = (datetime.now(UTC) + timedelta(hours=6)).replace(tzinfo=None).isoformat()
    resp = await client.post(f"/api/thermostats/{THERMO}/eco-suspend", json={"resume_at": naive})
    assert resp.status == 200
    body = await resp.json()
    assert body["resume_at"].endswith("+00:00")


@pytest.mark.asyncio
async def test_suspend_non_utc_offset_normalised(client) -> None:
    """A +02:00 offset must be converted, not stripped, on the way to storage."""
    await _configure_thermostat(client)
    offset_dt = (datetime.now(UTC) + timedelta(hours=6)).astimezone(UTC) + timedelta(hours=2)
    wire = offset_dt.replace(tzinfo=None).isoformat() + "+02:00"
    resp = await client.post(f"/api/thermostats/{THERMO}/eco-suspend", json={"resume_at": wire})
    assert resp.status == 200
    stored = datetime.fromisoformat((await resp.json())["resume_at"])
    assert stored == datetime.fromisoformat(wire)
    assert stored.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# State round-trip: set → visible → replace → clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suspend_round_trip_and_replace_and_clear(client) -> None:
    await _configure_thermostat(client)

    first = _future_iso(hours=4)
    resp = await client.post(f"/api/thermostats/{THERMO}/eco-suspend", json={"resume_at": first})
    assert resp.status == 200
    assert (await resp.json())["thermostat_entity_id"] == THERMO

    # Visible in the settings aggregate (the banner's single fetch)…
    settings = await (await client.get("/api/settings")).json()
    assert settings["eco_suspend"] == {THERMO: datetime.fromisoformat(first).isoformat()}

    # …and read-only on the thermostat list.
    thermostats = await (await client.get("/api/thermostats")).json()
    (tc,) = [t for t in thermostats if t["thermostat_entity_id"] == THERMO]
    assert tc["eco_suspend_until"] == datetime.fromisoformat(first).isoformat()

    # POSTing again replaces (the edit path) — still exactly one suspension.
    second = _future_iso(hours=12)
    resp = await client.post(f"/api/thermostats/{THERMO}/eco-suspend", json={"resume_at": second})
    assert resp.status == 200
    settings = await (await client.get("/api/settings")).json()
    assert settings["eco_suspend"] == {THERMO: datetime.fromisoformat(second).isoformat()}

    # DELETE clears; idempotent on repeat.
    assert (await client.delete(f"/api/thermostats/{THERMO}/eco-suspend")).status == 200
    settings = await (await client.get("/api/settings")).json()
    assert settings["eco_suspend"] == {}
    thermostats = await (await client.get("/api/thermostats")).json()
    assert thermostats[0]["eco_suspend_until"] is None
    assert (await client.delete(f"/api/thermostats/{THERMO}/eco-suspend")).status == 200


@pytest.mark.asyncio
async def test_suspend_config_put_does_not_clobber(client) -> None:
    """Editing the thermostat config must leave the suspension untouched —
    the state deliberately lives outside thermostat_configs."""
    await _configure_thermostat(client)
    until = _future_iso()
    await client.post(f"/api/thermostats/{THERMO}/eco-suspend", json={"resume_at": until})

    # A full config edit, including an eco_suspend_until field a stale client
    # might echo back — it must be ignored.
    resp = await client.put(
        f"/api/thermostats/{THERMO}",
        json={"name": "Renamed", "eco_suspend_until": None},
    )
    assert resp.status == 200

    settings = await (await client.get("/api/settings")).json()
    assert settings["eco_suspend"] == {THERMO: datetime.fromisoformat(until).isoformat()}


@pytest.mark.asyncio
async def test_delete_thermostat_removes_suspension(client) -> None:
    await _configure_thermostat(client)
    await client.post(f"/api/thermostats/{THERMO}/eco-suspend", json={"resume_at": _future_iso()})
    assert (await client.delete(f"/api/thermostats/{THERMO}")).status == 200
    # In-memory scheduler state is reloaded lazily from DB on restart; the DB
    # row must be gone so it can never resurrect.
    from backend import db as dbmod

    conn = client.app["scheduler"]._db_conn
    assert await dbmod.get_all_eco_suspensions(conn) == {}


@pytest.mark.asyncio
async def test_suspend_survives_scheduler_reload(client) -> None:
    """reload_db (backup restore path) re-hydrates suspensions from the table."""
    await _configure_thermostat(client)
    until = _future_iso()
    await client.post(f"/api/thermostats/{THERMO}/eco-suspend", json={"resume_at": until})

    sched = client.app["scheduler"]
    sched._eco_suspends = {}  # simulate cold in-memory state
    await sched.reload_db()

    settings = await (await client.get("/api/settings")).json()
    assert settings["eco_suspend"] == {THERMO: datetime.fromisoformat(until).isoformat()}


# ---------------------------------------------------------------------------
# Route-order regression (the {entity_id:.*} shadowing bug)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suffixed_delete_routes_not_shadowed_by_delete_thermostat(client) -> None:
    """DELETE /api/thermostats/{id}/eco-suspend (and /test-vacation) must reach
    their own handlers. The old {entity_id:.*} pattern on DELETE
    /api/thermostats/{id} matched slashes and, being registered first,
    swallowed every suffixed DELETE as a delete of a bogus entity id."""
    await _configure_thermostat(client)
    resp = await client.delete(f"/api/thermostats/{THERMO}/eco-suspend")
    body = await resp.json()
    assert resp.status == 200
    assert body == {"thermostat_entity_id": THERMO, "resume_at": None}
    assert "deleted" not in body

    # The thermostat itself must still exist.
    thermostats = await (await client.get("/api/thermostats")).json()
    assert [t["thermostat_entity_id"] for t in thermostats] == [THERMO]

    # And the plain DELETE still deletes.
    resp = await client.delete(f"/api/thermostats/{THERMO}")
    assert resp.status == 200
    assert (await resp.json())["deleted"] == THERMO


# ---------------------------------------------------------------------------
# Engine integration: gate + next-cycle-only semantics
# ---------------------------------------------------------------------------


async def _create_cooling_room(client, target_temp: float = 70.0) -> str:
    resp = await client.post("/api/rooms", json={"name": "Bedroom", "thermostat_entity_id": THERMO})
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.test_room_temp"})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": "cover.test_room_vent", "control_method": "open_close"},
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
            "target_temp": target_temp,
        },
    )
    return room_id


def _seed_warm_room(fake_ha, room_temp: float = 78.0) -> None:
    fake_ha.seed_state(
        THERMO,
        "cool",
        {"current_temperature": 78.0, "temperature": 76.0, "hvac_action": "idle"},
    )
    fake_ha.seed_state("sensor.test_room_temp", str(room_temp), {"unit_of_measurement": "°F"})
    fake_ha.seed_state("cover.test_room_vent", "open", {})


async def _configure_outdoor(client, fake_ha, temp_f: float) -> None:
    fake_ha.seed_state(OUTDOOR, str(temp_f), {"unit_of_measurement": "°F"})
    await client.put("/api/settings/outside-temp-entity", json={"entity_id": OUTDOOR})


# Step config: any outdoor >= 86 °F relaxes the 70 °F cooling target by the
# full 4 °F to exactly 74 °F (setpoint 72 with the 2 °F overshoot); when
# suspended the setpoint is the unrelaxed 68 °F.
_STEP_ECO = {
    "eco_mode_enabled": True,
    "eco_cooling_outdoor_threshold": 86,
    "eco_cooling_full_drift_temp": 86,
    "eco_cooling_max_drift": 4,
}


@pytest.mark.asyncio
async def test_suspended_cycle_runs_at_unrelaxed_target(client, fake_ha, tick) -> None:
    """Suspended zone on a hot day: the cycle behaves exactly as if Eco were
    off — unrelaxed setpoint, eco_active False in the cycle records."""
    _seed_warm_room(fake_ha)
    await _configure_outdoor(client, fake_ha, 95.0)
    await _create_cooling_room(client, target_temp=70.0)
    await client.put(f"/api/thermostats/{THERMO}", json=_STEP_ECO)
    resp = await client.post(
        f"/api/thermostats/{THERMO}/eco-suspend", json={"resume_at": _future_iso()}
    )
    assert resp.status == 200

    await tick()

    sp = fake_ha.calls_for("set_temperature")[-1].data["temperature"]
    assert sp == pytest.approx(68.0), "suspended: unrelaxed 70 target − 2 overshoot"

    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["eco_active"] is False
    detail = await (await client.get(f"/api/logs/{logs[0]['id']}/detail")).json()
    room = detail["rooms"][0]
    assert room["requested_target"] == pytest.approx(70.0)
    assert room["effective_target"] == pytest.approx(70.0)
    assert room["eco_active"] is False


@pytest.mark.asyncio
async def test_suspend_wins_over_room_opt_in_end_to_end(client, fake_ha, tick) -> None:
    """Thermostat Eco off + room-level opt-in normally relaxes; a suspension
    silences the room opt-in too (zone-wide)."""
    _seed_warm_room(fake_ha)
    await _configure_outdoor(client, fake_ha, 95.0)
    room_id = await _create_cooling_room(client, target_temp=70.0)
    # Thermostat-level Eco OFF, step params only.
    await client.put(
        f"/api/thermostats/{THERMO}",
        json={**_STEP_ECO, "eco_mode_enabled": False},
    )
    # Room-level explicit opt-in.
    resp = await client.put(f"/api/rooms/{room_id}", json={"eco_mode_enabled": True})
    assert resp.status == 200
    await client.post(f"/api/thermostats/{THERMO}/eco-suspend", json={"resume_at": _future_iso()})

    await tick()

    sp = fake_ha.calls_for("set_temperature")[-1].data["temperature"]
    assert sp == pytest.approx(68.0), "room opt-in must not survive the suspension"
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["eco_active"] is False


@pytest.mark.asyncio
async def test_suspend_mid_cycle_takes_effect_next_cycle_only(client, fake_ha, tick) -> None:
    """A cycle already running when the suspension lands keeps its relaxed
    target; the suspension only bites on the next fresh start."""
    _seed_warm_room(fake_ha)
    await _configure_outdoor(client, fake_ha, 95.0)
    await _create_cooling_room(client, target_temp=70.0)
    await client.put(f"/api/thermostats/{THERMO}", json=_STEP_ECO)

    await tick()  # cycle starts relaxed: setpoint 72
    sp = fake_ha.calls_for("set_temperature")[-1].data["temperature"]
    assert sp == pytest.approx(72.0)

    # Suspend while the cycle runs, then tick again.
    await client.post(f"/api/thermostats/{THERMO}/eco-suspend", json={"resume_at": _future_iso()})
    await tick()

    # No new setpoint command at the unrelaxed 68 — the cycle keeps its state.
    setpoints = [c.data["temperature"] for c in fake_ha.calls_for("set_temperature")]
    assert 68.0 not in setpoints, "running cycle must finish under its at-start Eco state"
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["eco_active"] is True


@pytest.mark.asyncio
async def test_clear_mid_cycle_takes_effect_next_cycle_only(client, fake_ha, tick) -> None:
    """Mirror image: a cycle started suspended stays unrelaxed even if the
    suspension is cleared (or expires) while it runs."""
    _seed_warm_room(fake_ha)
    await _configure_outdoor(client, fake_ha, 95.0)
    await _create_cooling_room(client, target_temp=70.0)
    await client.put(f"/api/thermostats/{THERMO}", json=_STEP_ECO)
    await client.post(f"/api/thermostats/{THERMO}/eco-suspend", json={"resume_at": _future_iso()})

    await tick()  # cycle starts suspended: setpoint 68
    sp = fake_ha.calls_for("set_temperature")[-1].data["temperature"]
    assert sp == pytest.approx(68.0)

    await client.delete(f"/api/thermostats/{THERMO}/eco-suspend")
    await tick()

    setpoints = [c.data["temperature"] for c in fake_ha.calls_for("set_temperature")]
    assert 72.0 not in setpoints, "running cycle must not start relaxing mid-flight"
    logs = await (await client.get("/api/logs")).json()
    assert len(logs) == 1 and logs[0]["eco_active"] is False


@pytest.mark.asyncio
async def test_expired_suspension_resumes_eco_on_next_cycle(client, fake_ha, tick) -> None:
    """Once the sweep clears an expired suspension, a fresh cycle relaxes
    again — the standing Eco config was never modified."""
    _seed_warm_room(fake_ha)
    await _configure_outdoor(client, fake_ha, 95.0)
    await _create_cooling_room(client, target_temp=70.0)
    await client.put(f"/api/thermostats/{THERMO}", json=_STEP_ECO)

    sched = client.app["scheduler"]
    # Plant an already-expired suspension directly (the API rejects past
    # datetimes by design), then let the tick's sweep clear it.
    from backend import db as dbmod

    expired = datetime.now(UTC) - timedelta(seconds=1)
    sched._eco_suspends[THERMO] = expired
    await dbmod.set_eco_suspension(sched._db_conn, THERMO, expired)

    await tick()

    assert sched.get_eco_suspend_until(THERMO) is None
    settings = await (await client.get("/api/settings")).json()
    assert settings["eco_suspend"] == {}
    # The cycle that started this tick relaxes normally.
    sp = fake_ha.calls_for("set_temperature")[-1].data["temperature"]
    assert sp == pytest.approx(72.0)
    logs = await (await client.get("/api/logs")).json()
    assert logs[0]["eco_active"] is True
