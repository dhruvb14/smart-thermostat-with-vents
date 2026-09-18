"""Ambient plausibility guard, end-to-end (Issue #636).

Reproduces the production incident through the real stack the operator
actually sees: a thermostat recovers from an outage reporting a glitched
``current_temperature`` for one tick, and the engine must not act on it.

  - "No plausibility guard on thermostat ambient" — the headline scenario:
    seed the thermostat ``unavailable``, return it reporting 32°F (the
    classic 0°C null glitch), assert no heat command is issued and the
    vacation hold's existing #627 ``unreadable:no-ambient`` posture is
    announced (naming the rejected value), then return a real 79°F and
    assert normal supervision resumes.
  - The ``include_thermostat_sensor`` room-temperature proxy (``_get_avg_temp``)
    is wired to the SAME guard: a room relying on it must not take a glitched
    reading as its room temperature either. See ``TestIncludeThermostatSensorProxyGuarded``.

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
async def test_two_reactive_ticks_close_together_do_not_falsely_reject_a_small_change(
    client, fake_ha, tick
) -> None:
    """Round-2 review finding (BLOCKING): the scheduler ticks an engine
    REACTIVELY on every HA state-change event for its own thermostat, not
    only on the scheduled 60s interval — so two ticks can land only seconds
    apart in real life (a chatty integration, or HA simply splitting one
    physical update into separate temperature/humidity/other-attribute
    events). Reproduced with two back-to-back REAL `tick()` calls and a
    small, realistic ambient step (72.0°F -> 72.5°F): the second tick must
    NOT reject 72.5°F as an impossible rate of change just because almost no
    real time separates it from the first tick's acceptance of 72.0°F.

    Before the fix (memoizing per `_do_tick` invocation instead of per real
    elapsed time), this exact sequence made a room relying on
    `include_thermostat_sensor` intermittently read `None` — dropping it out
    of active cycle control (`_add_safety_rooms`, the mode vote, the
    Dashboard) on a live house for no real reason.
    """
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
        json={"name": "Den", "thermostat_entity_id": THERMO, "include_thermostat_sensor": True},
    )
    assert resp.status in (200, 201), await resp.text()
    room_id: str = (await resp.json())["id"]
    await client.post(
        f"/api/rooms/{room_id}/vents",
        json={"entity_id": VENT, "control_method": "open_close"},
    )
    fake_ha.seed_state(VENT, "open", {})
    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 72.0, "temperature": None, "hvac_action": "idle"}
    )

    await tick()  # first tick: establishes the baseline at 72.0°F

    engine = client.app["scheduler"].get_engine(THERMO)
    assert engine is not None
    conn = client.app["scheduler"]._db_conn
    room = await _db.get_room(conn, room_id)
    assert room is not None
    assert engine._get_avg_temp(room) == pytest.approx(72.0)

    # A second tick fires moments later — as a reactive tick would — with a
    # completely normal small rise. No manipulation of any guard timestamp
    # here: the whole point is that essentially zero REAL time has elapsed.
    fake_ha.seed_state(
        THERMO, "off", {"current_temperature": 72.5, "temperature": None, "hvac_action": "idle"}
    )
    await tick()

    avg = engine._get_avg_temp(room)
    assert avg is not None, (
        "a normal 0.5°F change arriving moments after the previous tick must not "
        "be falsely rejected as an impossible rate of change and drop the room "
        "out of active cycle control"
    )


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
    # The real-time-windowed eval memo (round 2) is separate from the rate
    # check's own baseline timestamp above — without also clearing it, this
    # tick would just reuse the FIRST tick's cached 79.0°F (real elapsed
    # time between these two lines is milliseconds), never actually looking
    # at the glitch at all.
    engine._ambient_eval_at = None

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
    # The thermostat DID report a reading (32.0°F) — it was rejected, not
    # missing — so the announced message must say so and name the value
    # (issue #636 item 4), not claim "reports no current temperature".
    assert any("rejected its ambient reading" in m and "32.0" in m for m in warnings), warnings
    # Issue #636 round 2: a rejected reading gets its own posture, distinct
    # from a genuinely missing one, so the two can announce independently.
    assert engine._vacation_hold_posture == "unreadable:ambient-rejected"

    # The real reading returns.
    engine._ambient_eval_at = None
    await fake_ha.set_entity_state(
        THERMO, "off", {"current_temperature": 79.0, "temperature": None, "hvac_action": "idle"}
    )
    await tick()

    # The real reading returns — Issue #638: this is now a park into a
    # recovered direction rather than the old idempotent "stay off". No
    # completed cycle exists, so direction recovery falls back to bound
    # proximity: 79°F sits 6°F from the ceiling, 19°F from the floor →
    # "cool", parked at 79 + 2 (overshoot_delta) = 81.0. Two identical calls
    # are expected here, not a regression: the fake HA's
    # set_thermostat_temperature does not mutate its mirrored ``state`` to
    # match the ``hvac_mode`` kwarg (only set_thermostat_hvac_mode does, per
    # its own docstring), so the reactive tick this state change dispatches
    # and the explicit tick() below both still see hvac_mode reporting "off"
    # and both (harmlessly) issue the identical park.
    calls = fake_ha.calls_for("set_temperature")
    assert calls and all(
        c.data == {"entity_id": THERMO, "temperature": 81.0, "hvac_mode": "cool"} for c in calls
    ), f"79°F is inside the band — must park consistently in cool at 81.0°F; got {fake_ha.calls}"
    assert engine._vacation_hold_posture == "parked:cool:81.0", (
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
    # Clear the real-time-windowed eval memo too (round 2) — separate from
    # the rate check's own baseline timestamp above — so the next tick
    # actually evaluates the glitch instead of reusing the first tick's
    # cached 79.0°F.
    engine._ambient_eval_at = None

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


class TestIncludeThermostatSensorProxyGuarded:
    """The ``include_thermostat_sensor`` room-temperature proxy
    (``_get_avg_temp``) is wired to the SAME plausibility guard as the two
    no-demand supervision arms — issue #636's own test plan requires this
    ("an include_thermostat_sensor room must not take 32°F as its room
    temperature"), end to end through the real API + tick stack.
    """

    @pytest.mark.asyncio
    async def test_a_glitch_no_longer_reaches_the_room_proxy(self, client, fake_ha, tick) -> None:
        """A room relying SOLELY on the thermostat probe (no other sensors)
        must read as having no data at all when that probe glitches — not as
        32°F."""
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

        assert avg is None, f"the glitch must not become the room's temperature; got {avg}"

    @pytest.mark.asyncio
    async def test_a_real_sensor_still_reports_while_the_glitch_is_excluded(
        self, client, fake_ha, tick
    ) -> None:
        """A room with a real sensor AND include_thermostat_sensor must keep
        using its real sensor while the glitched probe reading is dropped —
        proving the fix subtracts the bad input rather than poisoning the
        whole average or silencing the room entirely."""
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
        await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": SENSOR})
        await client.post(
            f"/api/rooms/{room_id}/vents",
            json={"entity_id": VENT, "control_method": "open_close"},
        )
        fake_ha.seed_state(SENSOR, "70.0", {"unit_of_measurement": "°F"})
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

        assert avg == pytest.approx(70.0), (
            f"the real sensor must drive the average, not the rejected 32°F probe; got {avg}"
        )
