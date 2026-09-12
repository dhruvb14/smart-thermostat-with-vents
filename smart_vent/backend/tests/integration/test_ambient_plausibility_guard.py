"""Ambient plausibility guard, end-to-end (Issue #636).

Reproduces the production incident through the real stack the operator
actually sees: a thermostat recovers from an outage reporting a glitched
``current_temperature`` for one tick, and the engine must not act on it.

  - "No plausibility guard on thermostat ambient" — the headline scenario:
    seed the thermostat ``unavailable``, return it reporting 32°F (the
    classic 0°C null glitch), assert no heat command is issued and the
    vacation hold's existing #627 ``unreadable:no-ambient`` posture is
    announced, then return a real 79°F and assert normal supervision
    resumes.
  - A regression characterization for the ``include_thermostat_sensor`` room
    proxy, which this fix deliberately does NOT touch (see the module
    docstring on that test class for why).

Every temperature here is °F — the engine never converts (see CLAUDE.md).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend import db as _db

THERMO = "climate.test_thermostat"
SENSOR = "sensor.den_temp"
VENT = "cover.den_vent"


async def _configure(client, *, min_setpoint: float = 60.0, max_setpoint: float = 85.0) -> None:
    resp = await client.post(
        "/api/thermostats",
        json={
            "thermostat_entity_id": THERMO,
            "total_vents_count": 4,
            "min_setpoint": min_setpoint,
            "max_setpoint": max_setpoint,
            "deadband": 1.0,
            "vacation_hvac_mode": "single",
            "vacation_safety_cycles": False,
        },
    )
    assert resp.status in (200, 201), await resp.text()


async def _make_room(client, fake_ha) -> str:
    """A room with no schedule/presence, so the vacation hold (not a per-room
    safety cycle) is the only thing driving the thermostat."""
    resp = await client.post("/api/rooms", json={"name": "Den", "thermostat_entity_id": THERMO})
    room_id: str = (await resp.json())["id"]
    await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": SENSOR})
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": VENT, "control_method": "open_close"},
    )
    fake_ha.seed_state(SENSOR, "70.0", {"unit_of_measurement": "°F"})
    fake_ha.seed_state(VENT, "open", {})
    return room_id


async def _enable_vacation(client) -> None:
    return_at = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    resp = await client.post("/api/settings/vacation-mode", json={"return_at": return_at})
    assert resp.status == 200


async def _warnings(client) -> list[str]:
    events = await (await client.get("/api/logs/events?level=warning")).json()
    return [e["message"] for e in events]


@pytest.mark.asyncio
async def test_reconnect_glitch_is_rejected_then_normal_supervision_resumes(
    client, fake_ha, tick
) -> None:
    """The production incident, reproduced end-to-end.

    A 5m25s outage ends with the thermostat reporting exactly 32°F — 0°C, the
    classic null/zero glitch from an integration that has reconnected but not
    yet repopulated its attributes — 47°F colder than the real, pre-outage
    reading of 79°F and a real breach of the 60°F floor if trusted. The real
    reading returns on the next tick.
    """
    await _configure(client, min_setpoint=60.0, max_setpoint=85.0)
    await _make_room(client, fake_ha)
    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 79.0, "temperature": None, "hvac_action": "idle"}
    )
    await _enable_vacation(client)

    # Establish the real, pre-outage baseline.
    await tick()
    engine = client.app["scheduler"].get_engine(THERMO)
    assert engine is not None
    assert engine._last_valid_ambient_f == 79.0

    # The outage: the thermostat drops off HA entirely.
    await fake_ha.set_entity_state(THERMO, "unavailable", {})
    await tick()
    assert engine.unavailable_since is not None
    # Just past the 5-minute default abort threshold when it reconnects —
    # matching the production report ("just past unavailable_abort_after_min:
    # 5"). No cycle was running, so there is nothing to abort either way.
    engine._unavailable_since = datetime.now(UTC) - timedelta(minutes=5, seconds=25)
    # The rate check's own baseline ages by the same real-world amount, so
    # this proves the guard rejects the glitch at the ACTUAL incident timing
    # (47°F over ~5.4 real minutes is still ~8.7°F/min, well past the 3°F/min
    # limit) rather than merely because the test executes fast.
    engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=5, seconds=25)

    # Reconnects reporting the glitch.
    fake_ha.reset_calls()
    await fake_ha.set_entity_state(
        THERMO, "off", {"current_temperature": 32.0, "temperature": None, "hvac_action": "idle"}
    )
    await tick()

    assert engine.unavailable_since is None, "the entity IS reporting again"
    assert fake_ha.calls_for("set_temperature") == [], (
        f"the glitch must not command heat (or anything else); got {fake_ha.calls}"
    )
    warnings = await _warnings(client)
    assert any("no ambient reading" in m for m in warnings), warnings
    assert engine._vacation_hold_posture == "unreadable:no-ambient"

    # The real reading returns.
    await fake_ha.set_entity_state(
        THERMO, "off", {"current_temperature": 79.0, "temperature": None, "hvac_action": "idle"}
    )
    await tick()

    assert fake_ha.calls_for("set_temperature") == [], "79°F is inside the band — still no command"
    assert engine._vacation_hold_posture == "off:60.0:85.0", (
        "normal supervision must resume once a plausible reading returns"
    )


@pytest.mark.asyncio
async def test_reconnect_glitch_does_not_trip_the_safety_backstop(client, fake_ha, tick) -> None:
    """Same incident, non-vacation path: `_enforce_safety_setpoint` is the
    no-demand backstop outside of vacation mode, and must not treat the
    glitch as a real breach of `min_setpoint` either.
    """
    await _configure(client, min_setpoint=60.0, max_setpoint=85.0)
    await _make_room(client, fake_ha)
    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 79.0, "temperature": None, "hvac_action": "idle"}
    )

    await tick()  # establish the baseline; no vacation mode this time
    engine = client.app["scheduler"].get_engine(THERMO)
    assert engine is not None and engine._last_valid_ambient_f == 79.0

    await fake_ha.set_entity_state(THERMO, "unavailable", {})
    await tick()
    engine._unavailable_since = datetime.now(UTC) - timedelta(minutes=5, seconds=25)
    engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=5, seconds=25)

    fake_ha.reset_calls()
    await fake_ha.set_entity_state(
        THERMO, "off", {"current_temperature": 32.0, "temperature": None, "hvac_action": "idle"}
    )
    await tick()

    assert fake_ha.calls_for("set_temperature") == [], (
        f"32°F must not be trusted as a real breach of the 60°F floor; got {fake_ha.calls}"
    )
    warnings = await _warnings(client)
    assert any("ambient reading rejected" in m for m in warnings), warnings


class TestIncludeThermostatSensorProxyOutOfScope:
    """Characterizes the ``include_thermostat_sensor`` room-temperature proxy
    (``_get_avg_temp``, Issue #636's "Why this matters" section) as
    DELIBERATELY UNCHANGED by this fix.

    The issue's own Scope section puts only two consumers in scope for the
    validated accessor — the no-demand supervision arms
    ``_enforce_safety_setpoint`` and ``_apply_vacation_hold`` — and lists "the
    mode vote" and the rest of the active-cycle machinery as explicitly out of
    scope. ``_get_avg_temp`` feeds room temperatures into that same
    active-cycle machinery (schedule/presence demand, the mode vote, cycle
    start/join) from more than a dozen call sites, so wiring it to the new
    guard would be a materially broader behavioral change than "a single
    validated accessor used by the two no-demand arms" — exactly the surface
    the issue asks to keep out of a narrow, reviewable diff.

    This test pins today's (unfixed) behavior as a regression trip-wire: a
    room with ``include_thermostat_sensor`` on still takes a glitched
    ``current_temperature`` as its room temperature. It is intentionally NOT
    an assertion that this is safe — the issue is explicit that it is not —
    only a record that closing it is follow-up work, not silently expanded
    scope on this PR.
    """

    @pytest.mark.asyncio
    async def test_a_glitch_still_reaches_the_thermostat_sensor_room_proxy(
        self, client, fake_ha, tick
    ) -> None:
        resp = await client.post(
            "/api/thermostats",
            json={
                "thermostat_entity_id": THERMO,
                "total_vents_count": 4,
                "min_setpoint": 60.0,
                "max_setpoint": 85.0,
            },
        )
        assert resp.status in (200, 201), await resp.text()
        resp = await client.post(
            "/api/rooms",
            json={
                "name": "Den",
                "thermostat_entity_id": THERMO,
                "include_thermostat_sensor": True,
            },
        )
        assert resp.status in (200, 201), await resp.text()
        room_id: str = (await resp.json())["id"]
        await client.post(
            f"/api/rooms/{room_id}/vents",
            json={"entity_id": VENT, "control_method": "open_close"},
        )
        fake_ha.seed_state(VENT, "open", {})
        fake_ha.seed_state(
            THERMO, "off", {"current_temperature": 32.0, "temperature": None, "hvac_action": "idle"}
        )

        await tick()

        engine = client.app["scheduler"].get_engine(THERMO)
        assert engine is not None
        conn = client.app["scheduler"]._db_conn
        room = await _db.get_room(conn, room_id)
        assert room is not None
        avg = engine._get_avg_temp(room)

        assert avg == pytest.approx(32.0), (
            "known gap, out of scope for #636: the include_thermostat_sensor "
            "room-temperature proxy is unguarded by this fix"
        )
