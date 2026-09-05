"""Branch-coverage gaps in ``backend/engine/cycle_engine.py`` (source lines >= 2500).

Branch coverage was enabled after the suite already had ~100% statement
coverage, which exposed a set of guards whose *untaken* half had never been
exercised: the "no event logger configured" fallbacks scattered through the
reconciler and the safety paths, the thermostat-probe-missing arms of the
temperature helpers, the airflow-floor-blocked reconcile repairs, and the
``self._cycle_log is None`` arms of the overflow bookkeeping.

Every test drives the real engine method and asserts the BEHAVIOUR of the
untaken path — that the correction still happened (or was correctly withheld)
without the logger, that a missing probe reading is excluded rather than
crashing, that a deferred command is not sent — never merely that a line ran.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from backend import db
from backend.engine import room_manager as rm_mod
from backend.engine.cycle_engine import CycleEngine, CycleState
from backend.engine.room_manager import ActiveRoom, OverflowCandidate
from backend.engine.vent_controller import VentController
from backend.models import (
    CycleLog,
    Room,
    RoomCycleState,
    RoomOverride,
    RoomSensor,
    RoomVent,
    ThermostatConfig,
)

THERMO_ID = "climate.branch_b_thermostat"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _FakeHA:
    """Minimal HA client stand-in with per-entity state control.

    ``states`` maps entity_id → the dict ``get_state`` returns (``None``
    entries model an entity missing from the cache). Service calls are
    AsyncMocks so tests can assert what the engine commanded.
    """

    def __init__(self, states: dict[str, dict | None]) -> None:
        self.ha_temp_unit = "F"
        self.states = states
        self.ages: dict[str, float | None] = {}
        self.numeric: dict[str, float | None] = {}
        self.set_thermostat_temperature = AsyncMock()
        self.set_thermostat_temperature_range = AsyncMock()
        self.set_thermostat_hvac_mode = AsyncMock()
        self.open_cover = AsyncMock()
        self.close_cover = AsyncMock()
        self.set_cover_position = AsyncMock()
        self.set_cover_tilt_position = AsyncMock()
        self.toggle_cover = AsyncMock()

    def get_state(self, entity_id: str) -> dict | None:
        return self.states.get(entity_id)

    def get_state_age_seconds(self, entity_id: str) -> float | None:
        return self.ages.get(entity_id)

    def get_numeric_state(self, entity_id: str, max_age_min: float | None = None) -> float | None:
        return self.numeric.get(entity_id)


def _thermo_state(
    *,
    mode: str = "cool",
    ambient: float | None = 72.0,
    setpoint: float | None = 70.0,
) -> dict:
    attrs: dict = {}
    if ambient is not None:
        attrs["current_temperature"] = ambient
    if setpoint is not None:
        attrs["temperature"] = setpoint
    return {"state": mode, "attributes": attrs}


def _vent_state(open_: bool) -> dict:
    return {"state": "open" if open_ else "closed", "attributes": {}}


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict | None]] = []

    async def log(self, level, category, message, details=None):
        self.events.append((level, category, message, details))

    def messages(self) -> list[str]:
        return [m for _, _, m, _ in self.events]


def _make_engine(ha: _FakeHA, logger: _RecordingLogger | None = None) -> CycleEngine:
    engine = CycleEngine(
        thermostat_entity_id=THERMO_ID,
        ha=ha,  # type: ignore[arg-type]
        vent_ctrl=VentController(ha),  # type: ignore[arg-type]
        get_enabled=lambda: True,
    )
    engine._logger = logger  # type: ignore[assignment]
    return engine


async def _fresh_db(tc: ThermostatConfig | None = None) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    await db.upsert_thermostat_config(conn, tc or ThermostatConfig(thermostat_entity_id=THERMO_ID))
    return conn


async def _add_room(conn, room_id: str, name: str, *, vent: str | None = None) -> Room:
    room = Room(id=room_id, name=name, thermostat_entity_id=THERMO_ID)
    await db.upsert_room(conn, room)
    if vent is not None:
        await db.add_room_vent(conn, RoomVent.create(room_id, vent))
    return room


# ---------------------------------------------------------------------------
# _emit_sensor_freshness_warnings — the "no event logger" arms (2655, 2679)
# ---------------------------------------------------------------------------


class TestSensorFreshnessWithoutLogger:
    async def test_stale_sensor_is_still_marked_warned_without_a_logger(self):
        ha = _FakeHA({})
        ha.ages = {"sensor.a": 9999.0, "sensor.b": 60.0}
        engine = _make_engine(ha, logger=None)
        engine._sensor_map = {"r1": ["sensor.a", "sensor.b"]}
        room = Room(id="r1", name="Room One", thermostat_entity_id=THERMO_ID)
        active = {"r1": ActiveRoom(room=room, target_temp=70.0, source="schedule")}

        await engine._emit_sensor_freshness_warnings(active)

        # The stale sensor is recorded (so a later tick does not re-warn) and
        # the fresh one is untouched — the logger's absence changes neither.
        assert engine._stale_warned == {"sensor.a"}

    async def test_recovered_sensor_is_cleared_without_a_logger(self):
        ha = _FakeHA({})
        ha.ages = {"sensor.a": 60.0}
        engine = _make_engine(ha, logger=None)
        engine._sensor_map = {"r1": ["sensor.a"]}
        engine._stale_warned = {"sensor.a"}
        room = Room(id="r1", name="Room One", thermostat_entity_id=THERMO_ID)
        active = {"r1": ActiveRoom(room=room, target_temp=70.0, source="schedule")}

        await engine._emit_sensor_freshness_warnings(active)

        # Recovery clears the episode so a future staleness warns again.
        assert engine._stale_warned == set()


# ---------------------------------------------------------------------------
# _get_avg_temp / _sensor_counts — thermostat probe unusable (2703, 2710, 2738)
# ---------------------------------------------------------------------------


class TestThermostatProbeUnusable:
    def test_avg_ignores_probe_when_the_thermostat_entity_is_missing(self):
        ha = _FakeHA({THERMO_ID: None})
        ha.numeric = {"sensor.a": 68.0}
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["sensor.a"]}
        room = Room(
            id="r1",
            name="Room One",
            thermostat_entity_id=THERMO_ID,
            include_thermostat_sensor=True,
        )

        # Only the room sensor contributes — the absent probe is skipped, not
        # treated as a zero reading.
        assert engine._get_avg_temp(room) == 68.0

    def test_avg_ignores_probe_with_an_unparseable_current_temperature(self):
        ha = _FakeHA({THERMO_ID: _thermo_state(ambient=None)})
        ha.numeric = {"sensor.a": 68.0}
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["sensor.a"]}
        room = Room(
            id="r1",
            name="Room One",
            thermostat_entity_id=THERMO_ID,
            include_thermostat_sensor=True,
        )

        assert engine._get_avg_temp(room) == 68.0

    def test_sensor_counts_counts_a_probe_with_no_reading_as_unavailable(self):
        ha = _FakeHA({THERMO_ID: _thermo_state(ambient=None)})
        ha.numeric = {"sensor.a": 68.0}
        engine = _make_engine(ha)
        engine._sensor_map = {"r1": ["sensor.a"]}
        room = Room(
            id="r1",
            name="Room One",
            thermostat_entity_id=THERMO_ID,
            include_thermostat_sensor=True,
        )

        # 2 configured (sensor + probe), 1 reporting — the probe is counted in
        # the denominator but not the numerator.
        assert engine._sensor_counts(room) == (2, 1)


# ---------------------------------------------------------------------------
# _set_thermostat_setpoint — no ambient anchor / no logger (2793, 2800, 2868)
# ---------------------------------------------------------------------------


class TestSetSetpointWithoutAmbient:
    async def _drive(self, ha: _FakeHA, *, target: float, logger=None) -> None:
        engine = _make_engine(ha, logger=logger)
        room = Room(id="r1", name="Room One", thermostat_entity_id=THERMO_ID)
        engine._active_rooms = {"r1": ActiveRoom(room=room, target_temp=target, source="schedule")}
        tc = ThermostatConfig(thermostat_entity_id=THERMO_ID)
        await engine._set_thermostat_setpoint(tc, "cooling")
        self.ha = ha

    async def test_missing_thermostat_entity_skips_the_ambient_clamp(self):
        ha = _FakeHA({THERMO_ID: None})
        await self._drive(ha, target=72.0)

        # 72 − overshoot 2 = 70, unclamped: with no ambient reading there is
        # nothing to anchor against, and the command still goes out.
        ha.set_thermostat_temperature.assert_awaited_once_with(THERMO_ID, 70.0, hvac_mode="cool")

    async def test_unparseable_ambient_skips_the_ambient_clamp(self):
        ha = _FakeHA({THERMO_ID: _thermo_state(ambient=None)})
        await self._drive(ha, target=72.0)

        ha.set_thermostat_temperature.assert_awaited_once_with(THERMO_ID, 70.0, hvac_mode="cool")

    async def test_bounds_clamp_still_applies_without_a_logger(self):
        # ambient 72 → cooling ambient_bound 70; the derived setpoint (55−2=53)
        # is already below it, so only the min_setpoint bounds clamp fires.
        ha = _FakeHA({THERMO_ID: _thermo_state(ambient=72.0)})
        await self._drive(ha, target=55.0, logger=None)

        ha.set_thermostat_temperature.assert_awaited_once_with(THERMO_ID, 60.0, hvac_mode="cool")


# ---------------------------------------------------------------------------
# _reconcile_state — RUNNING repairs with no event logger
# (2984, 3036, 3054, 3095, 3126, 3157)
# ---------------------------------------------------------------------------


class TestReconcileRunningWithoutLogger:
    async def test_every_drift_is_repaired_with_no_logger_configured(self):
        tc = ThermostatConfig(thermostat_entity_id=THERMO_ID, has_bypass_damper=True)
        conn = await _fresh_db(tc)
        try:
            room_a = await _add_room(conn, "ra", "Room A", vent="cover.a")
            room_b = await _add_room(conn, "rb", "Room B", vent="cover.b")
            await _add_room(conn, "rc", "Room C (idle)", vent="cover.c")

            ha = _FakeHA(
                {
                    # Mode drifted heat vs the cycle-locked "cool", setpoint
                    # drifted 75 vs the 68 last sent.
                    THERMO_ID: _thermo_state(mode="heat", ambient=72.0, setpoint=75.0),
                    "cover.a": _vent_state(True),  # should be closed
                    "cover.b": _vent_state(False),  # should be open
                    "cover.c": _vent_state(True),  # idle room — should be closed
                }
            )
            engine = _make_engine(ha, logger=None)
            engine._state = CycleState.RUNNING
            engine._cycle_ha_mode = "cool"
            engine._last_setpoint_sent = 68.0
            engine._active_rooms = {
                "ra": ActiveRoom(room=room_a, target_temp=70.0, source="schedule"),
                "rb": ActiveRoom(room=room_b, target_temp=70.0, source="schedule"),
            }
            engine._room_vents = {
                "ra": await db.get_room_vents(conn, "ra"),
                "rb": await db.get_room_vents(conn, "rb"),
            }
            engine._room_cycle_states = {
                "ra": RoomCycleState(
                    cycle_id="c1",
                    room_id="ra",
                    target_temp=70.0,
                    vent_closed_at=datetime.now(UTC),
                ),
                "rb": RoomCycleState(cycle_id="c1", room_id="rb", target_temp=70.0),
            }

            await engine._reconcile_state(conn, tc)

            closed = {c.args[0] for c in ha.close_cover.await_args_list}
            opened = {c.args[0] for c in ha.open_cover.await_args_list}
            assert closed == {"cover.a", "cover.c"}
            assert opened == {"cover.b"}
            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 68.0, hvac_mode="cool"
            )
        finally:
            await conn.close()


class TestReconcileAirflowFloorBlocksRepair:
    async def test_close_deferred_by_the_airflow_floor_leaves_the_vents_open(self):
        # 2 total registers, all of which must stay open → the reconciler's
        # re-close is refused for both the active and the idle room.
        tc = ThermostatConfig(
            thermostat_entity_id=THERMO_ID,
            total_vents_count=2,
            min_open_vents_fraction=1.0,
        )
        conn = await _fresh_db(tc)
        try:
            room_a = await _add_room(conn, "ra", "Room A", vent="cover.a")
            await _add_room(conn, "rb", "Room B (idle)", vent="cover.b")

            ha = _FakeHA(
                {
                    THERMO_ID: _thermo_state(),
                    "cover.a": _vent_state(True),
                    "cover.b": _vent_state(True),
                }
            )
            logger = _RecordingLogger()
            engine = _make_engine(ha, logger=logger)
            engine._state = CycleState.RUNNING
            # Both None so the thermostat drift block is skipped entirely.
            engine._cycle_ha_mode = None
            engine._last_setpoint_sent = None
            engine._active_rooms = {
                "ra": ActiveRoom(room=room_a, target_temp=70.0, source="schedule")
            }
            engine._room_vents = {"ra": await db.get_room_vents(conn, "ra")}
            engine._room_cycle_states = {
                "ra": RoomCycleState(
                    cycle_id="c1",
                    room_id="ra",
                    target_temp=70.0,
                    vent_closed_at=datetime.now(UTC),
                )
            }

            await engine._reconcile_state(conn, tc)

            # No close command was issued, and no "re-closed" drift event was
            # claimed for a repair that never happened.
            ha.close_cover.assert_not_awaited()
            assert not any("re-closed" in m for m in logger.messages())
            # The thermostat drift block never ran, so nothing was re-asserted.
            ha.set_thermostat_temperature.assert_not_awaited()
        finally:
            await conn.close()


class TestReconcileThermostatDriftEdges:
    async def test_missing_thermostat_entity_skips_the_drift_check(self):
        tc = ThermostatConfig(thermostat_entity_id=THERMO_ID)
        conn = await _fresh_db(tc)
        try:
            ha = _FakeHA({THERMO_ID: None})
            engine = _make_engine(ha, logger=_RecordingLogger())
            engine._state = CycleState.RUNNING
            engine._last_setpoint_sent = 68.0
            engine._cycle_ha_mode = "cool"

            await engine._reconcile_state(conn, tc)

            # Nothing to compare against — the reconciler must not blind-fire a
            # re-assert at an unreachable thermostat.
            ha.set_thermostat_temperature.assert_not_awaited()
        finally:
            await conn.close()

    async def test_no_locked_mode_and_no_readable_setpoint_reasserts_nothing(self):
        tc = ThermostatConfig(thermostat_entity_id=THERMO_ID)
        conn = await _fresh_db(tc)
        try:
            # Thermostat present but reporting no `temperature` attribute.
            ha = _FakeHA({THERMO_ID: _thermo_state(setpoint=None)})
            engine = _make_engine(ha, logger=_RecordingLogger())
            engine._state = CycleState.RUNNING
            engine._cycle_ha_mode = None  # nothing to compare the mode against
            engine._last_setpoint_sent = 68.0

            await engine._reconcile_state(conn, tc)

            ha.set_thermostat_temperature.assert_not_awaited()
        finally:
            await conn.close()


class TestReconcileIdleWithoutLogger:
    async def test_idle_vent_reopen_and_out_of_bounds_setpoint_without_a_logger(self):
        tc = ThermostatConfig(thermostat_entity_id=THERMO_ID)
        conn = await _fresh_db(tc)
        try:
            await _add_room(conn, "ra", "Room A", vent="cover.a")
            ha = _FakeHA(
                {
                    # 95 °F sits above the 85 °F max_setpoint → the idle
                    # DB-settings drift warning path runs with no logger.
                    THERMO_ID: _thermo_state(mode="off", ambient=72.0, setpoint=95.0),
                    "cover.a": _vent_state(False),
                }
            )
            engine = _make_engine(ha, logger=None)
            engine._state = CycleState.IDLE

            await engine._reconcile_state(conn, tc)

            # The externally-closed vent is reopened even though there is no
            # event logger to narrate it.
            ha.open_cover.assert_awaited_once_with("cover.a")
        finally:
            await conn.close()

    async def test_unusable_configured_bounds_are_swallowed_not_raised(self):
        """A non-numeric ``min_setpoint`` must not take the reconciler down.

        This pins CURRENT behaviour of the ``except (ValueError, TypeError):
        pass`` guard on the idle DB-settings check — it documents the guard,
        it does not endorse silently swallowing a corrupt config value.
        """
        conn = await _fresh_db()
        try:
            await _add_room(conn, "ra", "Room A", vent="cover.a")
            ha = _FakeHA(
                {
                    THERMO_ID: _thermo_state(mode="off", ambient=72.0, setpoint=70.0),
                    "cover.a": _vent_state(True),
                }
            )
            logger = _RecordingLogger()
            engine = _make_engine(ha, logger=logger)
            engine._state = CycleState.IDLE

            bad_tc = ThermostatConfig(thermostat_entity_id=THERMO_ID)
            corrupt_field = "min_setpoint"
            setattr(bad_tc, corrupt_field, "warm")

            await engine._reconcile_state(conn, bad_tc)

            # The heartbeat still ran, but no bounds verdict was reached.
            assert any("Reconcile " in m for m in logger.messages())
            assert not any("outside configured bounds" in m for m in logger.messages())
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# restore_from_db — non-schedule/override sources, ambient unreadable
# (3359, 3368, 3390)
# ---------------------------------------------------------------------------


class TestRestoreSourceAndAmbientEdges:
    async def test_presence_and_expired_override_rooms_restore_with_defaults(self):
        conn = await _fresh_db()
        try:
            await _add_room(conn, "rp", "Presence Room", vent="cover.p")
            await _add_room(conn, "ro", "Override Room", vent="cover.o")

            cycle = CycleLog.create(
                thermostat_entity_id=THERMO_ID,
                mode="cooling",
                rooms_json=json.dumps(
                    {
                        "rp": {"name": "Presence Room", "target": 72.0, "source": "presence"},
                        "ro": {"name": "Override Room", "target": 71.0, "source": "override"},
                    }
                ),
            )
            await db.insert_cycle_log(conn, cycle)

            ha = _FakeHA(
                {
                    # Present, but with no readable ambient — the stale-mode
                    # sanity check cannot run, so the cycle is restored as-is.
                    THERMO_ID: _thermo_state(ambient=None),
                    "cover.p": _vent_state(True),
                    "cover.o": _vent_state(True),
                }
            )
            engine = _make_engine(ha, logger=None)

            await engine.restore_from_db(conn)

            assert engine._state == CycleState.RUNNING
            assert set(engine._active_rooms) == {"rp", "ro"}
            # Presence rooms carry no schedule band...
            assert engine._active_rooms["rp"].deadband_override is None
            # ...and an override whose hold row is gone keeps the default flag.
            assert engine._active_rooms["ro"].respect_eco is False
        finally:
            await conn.close()

    async def test_live_override_row_restores_respect_eco(self):
        """Companion to the above: with the hold row present the flag is read.

        Keeps the ``live_hold is None`` assertion honest by showing the other
        arm actually does something.
        """
        conn = await _fresh_db()
        try:
            await _add_room(conn, "ro", "Override Room", vent="cover.o")
            await db.set_room_override(
                conn,
                RoomOverride(
                    room_id="ro",
                    target_temp=71.0,
                    expires_at=datetime(2099, 1, 1, tzinfo=UTC),
                    respect_eco=True,
                ),
            )
            cycle = CycleLog.create(
                thermostat_entity_id=THERMO_ID,
                mode="cooling",
                rooms_json=json.dumps(
                    {"ro": {"name": "Override Room", "target": 71.0, "source": "override"}}
                ),
            )
            await db.insert_cycle_log(conn, cycle)

            ha = _FakeHA({THERMO_ID: _thermo_state(ambient=None), "cover.o": _vent_state(True)})
            engine = _make_engine(ha, logger=None)

            await engine.restore_from_db(conn)

            assert engine._active_rooms["ro"].respect_eco is True
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# Vacation hold + safety backstop deferrals with no logger (3629, 3871)
# ---------------------------------------------------------------------------


class TestOfftimeLockoutDeferralsWithoutLogger:
    async def test_vacation_hold_defers_cooling_during_the_lockout(self):
        tc = ThermostatConfig(thermostat_entity_id=THERMO_ID, min_cycle_offtime_min=10)
        conn = await _fresh_db(tc)
        try:
            ha = _FakeHA({})
            engine = _make_engine(ha, logger=None)
            engine._last_cycle_ended_at = datetime.now(UTC)

            state = _thermo_state(mode="off", ambient=90.0, setpoint=70.0)
            await engine._apply_vacation_hold(conn, state)

            # 90 °F is past the 85 °F ceiling, but the compressor lockout wins:
            # no cooling command is issued this tick.
            ha.set_thermostat_temperature.assert_not_awaited()
        finally:
            await conn.close()

    async def test_safety_backstop_defers_cooling_during_the_lockout(self):
        tc = ThermostatConfig(thermostat_entity_id=THERMO_ID, min_cycle_offtime_min=10)
        conn = await _fresh_db(tc)
        try:
            ha = _FakeHA({})
            engine = _make_engine(ha, logger=None)
            engine._last_cycle_ended_at = datetime.now(UTC)

            state = _thermo_state(mode="off", ambient=90.0, setpoint=70.0)
            breached = await engine._enforce_safety_setpoint(conn, state)

            # Still reported as a breach (so the caller knows the envelope is
            # violated) but the command is withheld.
            assert breached is True
            ha.set_thermostat_temperature.assert_not_awaited()
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _add_safety_rooms without a logger (3741)
# ---------------------------------------------------------------------------


class TestSafetyRoomsWithoutLogger:
    async def test_breaching_room_joins_the_active_map_without_a_logger(self):
        conn = await _fresh_db()
        try:
            await _add_room(conn, "rg", "Gym", vent="cover.g")
            await db.add_room_sensor(conn, RoomSensor.create(room_id="rg", entity_id="sensor.gym"))

            ha = _FakeHA({THERMO_ID: _thermo_state()})
            ha.numeric = {"sensor.gym": 95.0}
            engine = _make_engine(ha, logger=None)
            engine._sensor_map = {"rg": ["sensor.gym"]}

            new_active: dict[str, ActiveRoom] = {}
            await engine._add_safety_rooms(conn, new_active)

            assert set(new_active) == {"rg"}
            assert new_active["rg"].source == "safety"
            # max_setpoint 85 − deadband 0.5 → one band inside the breached cap.
            assert new_active["rg"].target_temp == pytest.approx(84.5)
            assert engine._safety_warned_room_ids == {"rg"}
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# Overflow bookkeeping with no open cycle log (4108, 4169, 4284, 4318, 4404)
# ---------------------------------------------------------------------------


class TestOverflowWithoutCycleLog:
    async def test_close_overflow_rooms_closes_vents_but_records_nothing(self):
        conn = await _fresh_db()
        try:
            await _add_room(conn, "ro", "Overflow Room", vent="cover.o")
            ha = _FakeHA({THERMO_ID: _thermo_state(), "cover.o": _vent_state(True)})
            engine = _make_engine(ha, logger=None)
            engine._cycle_log = None

            await engine._close_overflow_rooms(conn, {"ro"}, "no longer a candidate")

            ha.close_cover.assert_awaited_once_with("cover.o")
            cur = await conn.execute("SELECT COUNT(*) FROM cycle_vent_events")
            assert (await cur.fetchone())[0] == 0
        finally:
            await conn.close()

    async def test_min_runtime_hold_reopens_vents_but_records_nothing(self):
        conn = await _fresh_db()
        try:
            room = await _add_room(conn, "ra", "Room A", vent="cover.a")
            # A cycle row exists in the DB (the per-room state FKs to it) but
            # the engine's in-memory handle is missing — the untaken arm.
            cycle = CycleLog.create(thermostat_entity_id=THERMO_ID, mode="cooling", rooms_json="{}")
            await db.insert_cycle_log(conn, cycle)

            ha = _FakeHA({THERMO_ID: _thermo_state(), "cover.a": _vent_state(False)})
            engine = _make_engine(ha, logger=None)
            engine._cycle_log = None
            engine._active_rooms = {
                "ra": ActiveRoom(room=room, target_temp=70.0, source="schedule")
            }
            engine._room_vents = {"ra": await db.get_room_vents(conn, "ra")}
            rcs = RoomCycleState(
                cycle_id=cycle.id,
                room_id="ra",
                target_temp=70.0,
                vent_closed_at=datetime.now(UTC),
            )
            engine._room_cycle_states = {"ra": rcs}

            await engine._enter_min_runtime_hold(conn)

            ha.open_cover.assert_awaited_once_with("cover.a")
            assert rcs.vent_closed_at is None
            cur = await conn.execute("SELECT COUNT(*) FROM cycle_vent_events")
            assert (await cur.fetchone())[0] == 0
        finally:
            await conn.close()

    async def test_overflow_open_without_a_cycle_log_still_opens_the_vents(self, monkeypatch):
        conn = await _fresh_db()
        try:
            active = await _add_room(conn, "ra", "Room A", vent="cover.a")
            cand_room = await _add_room(conn, "rc", "Candidate", vent="cover.c")

            ha = _FakeHA(
                {
                    THERMO_ID: _thermo_state(),
                    "cover.a": _vent_state(True),
                    "cover.c": _vent_state(False),
                }
            )
            engine = _make_engine(ha, logger=None)
            engine._cycle_log = None
            engine._active_rooms = {
                "ra": ActiveRoom(room=active, target_temp=70.0, source="schedule")
            }
            engine._room_cycle_states = {
                "ra": RoomCycleState(cycle_id="c1", room_id="ra", target_temp=70.0)
            }

            candidate = OverflowCandidate(
                room=cand_room, current_temp=74.0, effective_setpoint=72.0, tier=1
            )

            async def _fake_candidates(*args, **kwargs):
                return [candidate]

            monkeypatch.setattr(rm_mod, "get_overflow_candidates", _fake_candidates)

            tc = ThermostatConfig(thermostat_entity_id=THERMO_ID)
            await engine._apply_overflow_during_hold(conn, "cooling", tc)

            ha.open_cover.assert_awaited_once_with("cover.c")
            assert engine._overflow_room_ids == {"rc"}
            cur = await conn.execute("SELECT COUNT(*) FROM cycle_vent_events")
            assert (await cur.fetchone())[0] == 0
        finally:
            await conn.close()

    async def test_no_newly_opened_room_logs_no_overflow_announcement(self, monkeypatch):
        """Only a swap-out happened: nothing was opened, so nothing is announced."""
        conn = await _fresh_db()
        try:
            active = await _add_room(conn, "ra", "Room A", vent="cover.a")
            keep = await _add_room(conn, "rk", "Kept Candidate", vent="cover.k")
            await _add_room(conn, "rd", "Dropped Candidate", vent="cover.d")

            ha = _FakeHA(
                {
                    THERMO_ID: _thermo_state(),
                    "cover.a": _vent_state(True),
                    "cover.k": _vent_state(True),  # already open → not re-opened
                    "cover.d": _vent_state(True),  # no longer a candidate → closed
                }
            )
            logger = _RecordingLogger()
            engine = _make_engine(ha, logger=logger)
            engine._cycle_log = None
            engine._overflow_room_ids = {"rk", "rd"}
            engine._active_rooms = {
                "ra": ActiveRoom(room=active, target_temp=70.0, source="schedule")
            }
            engine._room_cycle_states = {
                "ra": RoomCycleState(cycle_id="c1", room_id="ra", target_temp=70.0)
            }

            candidate = OverflowCandidate(
                room=keep, current_temp=74.0, effective_setpoint=72.0, tier=2
            )

            async def _fake_candidates(*args, **kwargs):
                return [candidate]

            monkeypatch.setattr(rm_mod, "get_overflow_candidates", _fake_candidates)

            tc = ThermostatConfig(thermostat_entity_id=THERMO_ID)
            await engine._apply_overflow_during_hold(conn, "cooling", tc)

            ha.close_cover.assert_awaited_once_with("cover.d")
            ha.open_cover.assert_not_awaited()
            assert engine._overflow_room_ids == {"rk"}
            assert not any("Overflow conditioning" in m for m in logger.messages())
        finally:
            await conn.close()

    async def test_finalize_overflow_rooms_clears_state_with_no_cycle_log(self):
        conn = await _fresh_db()
        try:
            ha = _FakeHA({THERMO_ID: _thermo_state()})
            engine = _make_engine(ha, logger=None)
            engine._cycle_log = None
            rcs = RoomCycleState(cycle_id="c1", room_id="ro", target_temp=72.0, role="overflow")
            engine._overflow_room_states = {"ro": rcs}

            await engine._finalize_overflow_rooms(conn)

            # Bookkeeping is cleared, but with no open cycle log there is
            # nothing to write an end temperature onto.
            assert engine._overflow_room_states == {}
            assert rcs.temp_at_end is None
        finally:
            await conn.close()
