"""
Cycle-engine coverage gaps — RESTORE, VACATION HOLD, SAFETY BACKSTOP and the
MINIMUM-RUNTIME-HOLD / OVERFLOW machinery.

Companion to ``test_cycle_engine.py`` and ``test_cycle_engine_gaps_a.py``; this
file targets the defensive and rarely-taken branches at the tail of
``cycle_engine.py``:

  * ``restore_from_db`` — the off-time-lockout rehydrate (#432), duplicate
    open-log cleanup, a corrupt ``rooms_json`` snapshot, and the physical
    cleanup that follows discarding a stale cycle (#429)
  * ``_apply_vacation_hold`` — the unavailable-thermostat bail-out, the
    already-holding idempotence skips (#434/#296) and every HA-failure path
  * ``_enforce_safety_setpoint`` — the two fail-safe bail-outs (#367)
  * ``_rooms_drifted_past_deadband`` / ``_drifted_past_deadband`` — rooms that
    cannot vote, and the "unexpected mode never counts as drifted" contract
  * ``_release_min_runtime_hold`` / ``_enter_min_runtime_hold`` /
    ``_close_overflow_rooms`` / ``_apply_overflow_during_hold`` /
    ``_record_overflow_open`` — the diagnostics writes that must never break a
    tick, and the guards that make each of them a no-op (#237/#254/#423)

Every temperature here is °F — the engine never converts (see CLAUDE.md).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from backend import db
from backend.engine import room_manager
from backend.engine.cycle_engine import CycleEngine, CycleState, _drifted_past_deadband
from backend.engine.room_manager import ActiveRoom, OverflowCandidate
from backend.engine.vent_controller import VentController
from backend.models import (
    CycleLog,
    Room,
    RoomCycleState,
    RoomVent,
    ThermostatConfig,
)

THERMO_ID = "climate.test_thermostat"
ENGINE_LOGGER = "backend.engine.cycle_engine"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_ha(
    ambient: float | None = 72.0,
    hvac_mode: str = "cool",
    setpoint: float | None = 72.0,
    *,
    cover_state: str = "open",
    extra_attrs: dict | None = None,
) -> MagicMock:
    """Mock HAClient whose ``get_state`` routes cover.* separately from the
    thermostat, so vent-state reads behave like a live zone."""
    thermo = {
        "state": hvac_mode,
        "attributes": {
            "current_temperature": ambient,
            "temperature": setpoint,
            **(extra_attrs or {}),
        },
    }
    ha = MagicMock()
    ha.ha_temp_unit = "F"

    def _get_state(entity_id: str):
        if entity_id.startswith("cover."):
            return {"state": cover_state, "attributes": {}}
        return thermo

    ha.get_state.side_effect = _get_state
    ha.get_numeric_state.return_value = None
    ha.get_state_age_seconds.return_value = None
    ha.set_thermostat_temperature = AsyncMock()
    ha.set_thermostat_temperature_range = AsyncMock()
    ha.set_thermostat_hvac_mode = AsyncMock()
    ha.open_cover = AsyncMock()
    ha.close_cover = AsyncMock()
    ha.call_service = AsyncMock()
    return ha


def _make_engine(
    ha: MagicMock,
    logger: AsyncMock | None = None,
    *,
    vacation: bool = False,
) -> CycleEngine:
    return CycleEngine(
        thermostat_entity_id=THERMO_ID,
        ha=ha,
        vent_ctrl=VentController(ha),
        event_logger=logger,
        get_enabled=lambda: True,
        get_vacation_mode=lambda: vacation,
    )


def _tc(**overrides) -> ThermostatConfig:
    base: dict = {
        "thermostat_entity_id": THERMO_ID,
        "overshoot_delta": 2.0,
        "deadband": 0.5,
        "min_setpoint": 60.0,
        "max_setpoint": 85.0,
    }
    base.update(overrides)
    return ThermostatConfig(**base)


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    return conn


async def _add_room(
    conn: aiosqlite.Connection,
    room_id: str,
    name: str,
    *,
    vents: list[str] | None = None,
) -> Room:
    room = Room.create(name=name, thermostat_entity_id=THERMO_ID)
    room.id = room_id
    await db.upsert_room(conn, room)
    for entity_id in vents or []:
        await db.add_room_vent(conn, RoomVent.create(room_id, entity_id))
    return room


def _ar(room: Room, target: float = 74.0, source: str = "schedule") -> ActiveRoom:
    return ActiveRoom(room=room, target_temp=target, source=source)


async def _open_cycle(
    conn: aiosqlite.Connection,
    rooms: dict[str, float],
    *,
    mode: str = "cooling",
    started_at: datetime | None = None,
) -> CycleLog:
    """Insert an OPEN cycle log whose rooms_json names ``rooms`` (id → target)."""
    cycle = CycleLog.create(
        thermostat_entity_id=THERMO_ID,
        mode=mode,
        rooms_json=json.dumps({rid: {"name": rid, "target": t} for rid, t in rooms.items()}),
    )
    if started_at is not None:
        cycle.started_at = started_at
    await db.insert_cycle_log(conn, cycle)
    return cycle


async def _running_engine(
    ha: MagicMock,
    conn: aiosqlite.Connection,
    rooms: dict[str, ActiveRoom],
    *,
    mode: str = "cooling",
    logger: AsyncMock | None = None,
    vacation: bool = False,
) -> tuple[CycleEngine, CycleLog]:
    """An engine parked in RUNNING with an open cycle log for ``rooms``."""
    engine = _make_engine(ha, logger, vacation=vacation)
    cycle = CycleLog.create(
        thermostat_entity_id=THERMO_ID,
        mode=mode,
        rooms_json=json.dumps(
            {rid: {"name": ar.room.name, "target": ar.target_temp} for rid, ar in rooms.items()}
        ),
    )
    await db.insert_cycle_log(conn, cycle)
    engine._state = CycleState.RUNNING
    engine._cycle_log = cycle
    engine._cycle_mode = mode
    engine._cycle_ha_mode = "cool" if mode == "cooling" else "heat"
    engine._active_rooms = dict(rooms)
    engine._room_cycle_states = {}
    for rid, ar in rooms.items():
        rcs = RoomCycleState(cycle_id=cycle.id, room_id=rid, target_temp=ar.target_temp)
        engine._room_cycle_states[rid] = rcs
        await db.upsert_room_cycle_state(conn, rcs)
        engine._room_vents[rid] = await db.get_room_vents(conn, rid)
    return engine, cycle


# ---------------------------------------------------------------------------
# restore_from_db — off-time lockout rehydrate + duplicate log cleanup
# ---------------------------------------------------------------------------


class TestRestoreHousekeeping:
    @pytest.mark.asyncio
    async def test_lockout_rehydrate_failure_is_logged_and_restore_continues(self, caplog):
        """A DB failure reading the last cycle end (#432) must not abort restore.

        The lockout clock is a nice-to-have on startup; losing it costs one
        compressor-protection window, whereas raising here would leave the
        engine IDLE with an open cycle log and an unsupervised thermostat.
        """
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            await _open_cycle(conn, {"r1": 72.0})
            engine = _make_engine(_make_ha(ambient=76.0))

            with (
                patch.object(db, "get_latest_cycle_end", side_effect=RuntimeError("db is gone")),
                caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER),
            ):
                await engine.restore_from_db(conn)

            assert any(
                "Failed to rehydrate off-time lockout clock" in r.message for r in caplog.records
            ), caplog.text
            assert engine._last_cycle_ended_at is None
            # Restore still completed — the cycle resumed.
            assert engine.cycle_state == CycleState.RUNNING
            assert set(engine._active_rooms) == {"r1"}
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_lockout_clock_is_rehydrated_when_the_read_succeeds(self):
        """Control for the test above: the happy path really does adopt the
        persisted end time, so the failure test is not passing vacuously."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            ended = datetime.now(UTC) - timedelta(minutes=3)
            prior = await _open_cycle(conn, {"r1": 72.0})
            await db.close_cycle_log(conn, prior.id, ended, ended_reason="completed")
            await _open_cycle(conn, {"r1": 72.0})
            engine = _make_engine(_make_ha(ambient=76.0))

            await engine.restore_from_db(conn)

            assert engine._last_cycle_ended_at is not None
            assert abs((engine._last_cycle_ended_at - ended).total_seconds()) < 1
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_duplicate_open_logs_are_closed_and_reported_to_the_event_log(self):
        """Two open logs for one thermostat (the pre-fix duplicate-cycle bug):
        restore keeps the newest, closes the rest, and says so in the event log
        — the UI would otherwise show two "Active" rows forever."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            now = datetime.now(UTC)
            stale = await _open_cycle(conn, {"r1": 72.0}, started_at=now - timedelta(minutes=30))
            newest = await _open_cycle(conn, {"r1": 72.0}, started_at=now - timedelta(minutes=2))
            logger = AsyncMock()
            engine = _make_engine(_make_ha(ambient=76.0), logger)

            await engine.restore_from_db(conn)

            open_ids = [c.id for c in await db.get_open_cycle_logs(conn, THERMO_ID)]
            assert open_ids == [newest.id], "only the newest open log should survive"
            assert engine._cycle_log is not None and engine._cycle_log.id == newest.id
            messages = [c.args[2] for c in logger.log.await_args_list]
            assert any("duplicate open cycle log" in m for m in messages), messages
            # The stale row is closed, not deleted.
            all_logs = await db.get_cycle_logs(conn, limit=10)
            assert any(c.id == stale.id and c.ended_at is not None for c in all_logs)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_corrupt_rooms_json_restores_a_cycle_with_no_active_rooms(self):
        """An unparseable ``rooms_json`` degrades to an empty snapshot rather
        than raising out of startup: the cycle resumes (so the timeout monitor
        and reconciler supervise the running HVAC) with no rooms."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            cycle = CycleLog.create(
                thermostat_entity_id=THERMO_ID, mode="cooling", rooms_json="{not json at all"
            )
            await db.insert_cycle_log(conn, cycle)
            engine = _make_engine(_make_ha(ambient=76.0))

            await engine.restore_from_db(conn)

            assert engine.cycle_state == CycleState.RUNNING
            assert engine._active_rooms == {}
            assert engine._cycle_log is not None and engine._cycle_log.id == cycle.id
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# restore_from_db — discarding a stale cycle and its physical cleanup (#429)
# ---------------------------------------------------------------------------


class TestRestoreDiscardsStaleCycle:
    """A persisted cycle whose direction contradicts the thermostat's current
    ambient is discarded on restore; the thermostat is parked on the idle side
    and every zone vent is re-opened, because nothing else supervises it once
    the cycle log is closed."""

    @staticmethod
    async def _setup(conn: aiosqlite.Connection, **tc_kwargs) -> CycleLog:
        await db.upsert_thermostat_config(conn, _tc(**tc_kwargs))
        await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
        await _add_room(conn, "r2", "Office", vents=["cover.r2"])
        # A heating cycle for rooms targeting 70°F, restored while the
        # thermostat reads 80°F — the space no longer needs heat.
        return await _open_cycle(conn, {"r1": 70.0, "r2": 68.0}, mode="heating")

    @pytest.mark.asyncio
    async def test_discard_parks_setpoint_reopens_vents_and_logs_the_event(self):
        conn = await _conn()
        try:
            cycle = await self._setup(conn)
            logger = AsyncMock()
            ha = _make_ha(ambient=80.0, hvac_mode="heat", cover_state="closed")
            engine = _make_engine(ha, logger)

            await engine.restore_from_db(conn)

            # The cycle was closed with the dedicated reason and the engine is
            # clean IDLE — the next tick infers a fresh direction.
            closed = (await db.get_cycle_logs(conn, limit=5))[0]
            assert closed.id == cycle.id
            assert closed.ended_reason == "discarded_stale_on_restore"
            assert engine.cycle_state == CycleState.IDLE
            assert engine._cycle_log is None and engine._active_rooms == {}
            assert engine._cycle_mode is None and engine._cycle_ha_mode is None
            assert engine._overflow_room_states == {} and engine._overflow_room_ids == set()
            # Parked on the idle side of heating: ambient − overshoot.
            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 78.0, hvac_mode="heat"
            )
            assert engine._last_setpoint_sent == 78.0
            # Every zone vent re-opened (both rooms), not just the cycle's.
            opened = {c.args[0] for c in ha.open_cover.await_args_list}
            assert opened == {"cover.r1", "cover.r2"}, ha.open_cover.await_args_list
            # The off-time lockout is armed for the run that just ended.
            assert engine._last_cycle_ended_at is not None
            messages = [c.args[2] for c in logger.log.await_args_list]
            assert any("discarding stale heating cycle" in m for m in messages), messages
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_park_failure_is_logged_and_vents_are_still_reopened(self, caplog):
        """A thermostat write failure must not skip the vent re-open — the two
        cleanups are independently guarded."""
        conn = await _conn()
        try:
            await self._setup(conn)
            ha = _make_ha(ambient=80.0, hvac_mode="heat", cover_state="closed")
            ha.set_thermostat_temperature.side_effect = RuntimeError("HA unreachable")
            engine = _make_engine(ha)

            with caplog.at_level(logging.ERROR, logger=ENGINE_LOGGER):
                await engine.restore_from_db(conn)

            assert any("failed to park setpoint" in r.message for r in caplog.records), caplog.text
            assert engine._last_setpoint_sent is None
            opened = {c.args[0] for c in ha.open_cover.await_args_list}
            assert opened == {"cover.r1", "cover.r2"}
            assert engine.cycle_state == CycleState.IDLE
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_vent_reopen_failure_is_logged_and_restore_still_finishes(self, caplog):
        conn = await _conn()
        try:
            await self._setup(conn)
            ha = _make_ha(ambient=80.0, hvac_mode="heat")
            engine = _make_engine(ha)
            engine._vent.open_room_vents = AsyncMock(
                side_effect=RuntimeError("cover service failed")
            )

            with caplog.at_level(logging.ERROR, logger=ENGINE_LOGGER):
                await engine.restore_from_db(conn)

            assert any("failed to reopen zone vents" in r.message for r in caplog.records), (
                caplog.text
            )
            # The setpoint park (which runs first) still happened, and the
            # engine still reached clean IDLE.
            ha.set_thermostat_temperature.assert_awaited_once()
            assert engine.cycle_state == CycleState.IDLE
            assert engine._cycle_log is None
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_a_failure_inside_the_sanity_check_never_escapes_restore(self):
        """``restore_from_db`` runs during app startup, so nothing in the
        stale-cycle sanity check may propagate: a raising ``close_cycle_log``
        is swallowed and the engine falls through to resuming the cycle."""
        conn = await _conn()
        try:
            await self._setup(conn)
            ha = _make_ha(ambient=80.0, hvac_mode="heat")
            engine = _make_engine(ha)

            with patch.object(db, "close_cycle_log", side_effect=ValueError("boom")):
                await engine.restore_from_db(conn)

            # Discard aborted mid-way → the cycle is resumed instead, and no
            # physical cleanup was performed.
            assert engine.cycle_state == CycleState.RUNNING
            ha.set_thermostat_temperature.assert_not_awaited()
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _apply_vacation_hold
# ---------------------------------------------------------------------------


class TestVacationHoldGuards:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "thermo_state",
        [None, {"state": "unavailable", "attributes": {}}],
        ids=["missing", "unavailable"],
    )
    async def test_no_commands_when_the_thermostat_is_not_readable(self, thermo_state):
        """An unreachable thermostat gets no vacation commands at all — not
        even a "turn off" — because we cannot tell what it is doing."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            ha = _make_ha()
            engine = _make_engine(ha)

            await engine._apply_vacation_hold(conn, thermo_state)

            ha.set_thermostat_temperature.assert_not_awaited()
            ha.set_thermostat_temperature_range.assert_not_awaited()
            ha.set_thermostat_hvac_mode.assert_not_awaited()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_range_mode_skips_the_write_when_already_holding(self):
        """Idempotence (#434/#296): re-commanding the identical range every
        60 s for a week of vacation is thousands of pointless writes."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, _tc(vacation_hvac_mode="range", min_setpoint=62.0, max_setpoint=80.0)
            )
            ha = _make_ha()
            engine = _make_engine(ha)
            state = {
                "state": "heat_cool",
                "attributes": {"target_temp_low": 62.0, "target_temp_high": 80.0},
            }

            await engine._apply_vacation_hold(conn, state)

            ha.set_thermostat_temperature_range.assert_not_awaited()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_range_mode_rewrites_when_the_thermostat_drifted(self):
        """Control for the skip above: a drifted high bound IS re-commanded."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, _tc(vacation_hvac_mode="range", min_setpoint=62.0, max_setpoint=80.0)
            )
            ha = _make_ha()
            engine = _make_engine(ha)
            state = {
                "state": "heat_cool",
                "attributes": {"target_temp_low": 62.0, "target_temp_high": 74.0},
            }

            await engine._apply_vacation_hold(conn, state)

            ha.set_thermostat_temperature_range.assert_awaited_once_with(THERMO_ID, 62.0, 80.0)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_range_mode_write_failure_is_logged_not_raised(self, caplog):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, _tc(vacation_hvac_mode="range", min_setpoint=62.0, max_setpoint=80.0)
            )
            ha = _make_ha()
            ha.set_thermostat_temperature_range.side_effect = RuntimeError("HA unreachable")
            engine = _make_engine(ha)

            with caplog.at_level(logging.ERROR, logger=ENGINE_LOGGER):
                await engine._apply_vacation_hold(conn, {"state": "off", "attributes": {}})

            assert any("failed to set range" in r.message for r in caplog.records), caplog.text
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_unreadable_ambient_turns_the_thermostat_off_and_logs_a_failure(self, caplog):
        """No usable ambient in single-setpoint mode → command "off"; if that
        command fails the tick still completes."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            ha = _make_ha()
            ha.set_thermostat_hvac_mode.side_effect = RuntimeError("HA unreachable")
            engine = _make_engine(ha)

            with caplog.at_level(logging.ERROR, logger=ENGINE_LOGGER):
                await engine._apply_vacation_hold(
                    conn, {"state": "cool", "attributes": {"current_temperature": None}}
                )

            ha.set_thermostat_hvac_mode.assert_awaited_once_with(THERMO_ID, "off")
            assert any("failed to turn off" in r.message for r in caplog.records), caplog.text
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_below_min_skips_the_write_when_already_heating_at_the_floor(self):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            engine = _make_engine(ha)
            state = {
                "state": "heat",
                "attributes": {"current_temperature": 55.0, "temperature": 62.0},
            }

            await engine._apply_vacation_hold(conn, state)

            ha.set_thermostat_temperature.assert_not_awaited()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_below_min_heat_command_failure_is_logged(self, caplog):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            ha.set_thermostat_temperature.side_effect = RuntimeError("HA unreachable")
            engine = _make_engine(ha)
            state = {
                "state": "off",
                "attributes": {"current_temperature": 55.0, "temperature": 70.0},
            }

            with caplog.at_level(logging.ERROR, logger=ENGINE_LOGGER):
                await engine._apply_vacation_hold(conn, state)

            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 62.0, hvac_mode="heat"
            )
            assert any("failed to heat" in r.message for r in caplog.records), caplog.text
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_above_max_skips_the_write_when_already_cooling_at_the_ceiling(self, caplog):
        """The already-holding skip must be checked BEFORE the compressor
        lockout gate — an in-progress hold is not a new compressor start, so it
        must return silently rather than log a deferral for a hold that is
        already running."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, _tc(min_setpoint=62.0, max_setpoint=80.0, min_cycle_offtime_min=10)
            )
            ha = _make_ha()
            engine = _make_engine(ha)
            engine._last_cycle_ended_at = datetime.now(UTC)  # inside the lockout
            state = {
                "state": "cool",
                "attributes": {"current_temperature": 88.0, "temperature": 80.0},
            }

            with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
                await engine._apply_vacation_hold(conn, state)

            ha.set_thermostat_temperature.assert_not_awaited()
            assert not any("deferred" in r.message for r in caplog.records), caplog.text
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_above_max_cool_command_failure_is_logged(self, caplog):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            ha.set_thermostat_temperature.side_effect = RuntimeError("HA unreachable")
            engine = _make_engine(ha)
            state = {
                "state": "off",
                "attributes": {"current_temperature": 88.0, "temperature": 70.0},
            }

            with caplog.at_level(logging.ERROR, logger=ENGINE_LOGGER):
                await engine._apply_vacation_hold(conn, state)

            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 80.0, hvac_mode="cool"
            )
            assert any("failed to cool" in r.message for r in caplog.records), caplog.text
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_inside_the_band_turn_off_failure_is_logged(self, caplog):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            ha.set_thermostat_hvac_mode.side_effect = RuntimeError("HA unreachable")
            engine = _make_engine(ha)
            state = {
                "state": "cool",
                "attributes": {"current_temperature": 70.0, "temperature": 70.0},
            }

            with caplog.at_level(logging.ERROR, logger=ENGINE_LOGGER):
                await engine._apply_vacation_hold(conn, state)

            ha.set_thermostat_hvac_mode.assert_awaited_once_with(THERMO_ID, "off")
            assert any("failed to turn off" in r.message for r in caplog.records), caplog.text
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _enforce_safety_setpoint — the fail-safe bail-outs (#367)
# ---------------------------------------------------------------------------


class TestSafetyBackstopBailouts:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "thermo_state",
        [None, {"state": "unavailable", "attributes": {"current_temperature": 99.0}}],
        ids=["missing", "unavailable"],
    )
    async def test_unreadable_thermostat_reports_no_breach(self, thermo_state):
        """Returning False keeps the caller on its normal idle path; an
        "unavailable" entity can still carry a stale attribute, so the state
        check must win over the temperature."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            engine = _make_engine(ha)

            assert await engine._enforce_safety_setpoint(conn, thermo_state) is False
            ha.set_thermostat_temperature.assert_not_awaited()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_missing_ambient_reading_reports_no_breach(self):
        """No usable ambient → do nothing rather than command the HVAC off a
        value we cannot trust."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            engine = _make_engine(ha)
            state = {"state": "off", "attributes": {"current_temperature": None}}

            assert await engine._enforce_safety_setpoint(conn, state) is False
            ha.set_thermostat_temperature.assert_not_awaited()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_a_readable_breach_still_engages(self):
        """Control: with a readable ambient over the ceiling the backstop does
        fire, so the two bail-outs above are discriminating on readability."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            engine = _make_engine(ha)
            state = {"state": "off", "attributes": {"current_temperature": 88.0}}

            assert await engine._enforce_safety_setpoint(conn, state) is True
            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 80.0, hvac_mode="cool"
            )
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# Drift predicates (#423)
# ---------------------------------------------------------------------------


class TestDriftPredicates:
    def test_unexpected_hvac_mode_never_counts_as_drifted(self):
        """The shared predicate is called with the *cycle* mode; an "off" or
        "unknown" mode must not silently pick a comparison direction."""
        assert _drifted_past_deadband(90.0, 70.0, "cooling", 0.5) is True
        assert _drifted_past_deadband(50.0, 70.0, "heating", 0.5) is True
        for mode in ("off", "unknown", "fan_only", ""):
            assert _drifted_past_deadband(90.0, 70.0, mode, 0.5) is False
            assert _drifted_past_deadband(50.0, 70.0, mode, 0.5) is False

    @pytest.mark.asyncio
    async def test_rooms_without_a_reading_or_a_cycle_state_cannot_vote(self):
        """Only rooms with BOTH a per-room cycle state and a live average temp
        may release the min-runtime hold — a sensor that dropped off must not
        be read as "drifted"."""
        conn = await _conn()
        try:
            drifted_room = await _add_room(conn, "hot", "Sun Room")
            no_state_room = await _add_room(conn, "nostate", "Attic")
            no_temp_room = await _add_room(conn, "notemp", "Cellar")
            ha = _make_ha()
            engine, _cycle = await _running_engine(
                ha,
                conn,
                {
                    "hot": _ar(drifted_room, target=70.0),
                    "nostate": _ar(no_state_room, target=70.0),
                    "notemp": _ar(no_temp_room, target=70.0),
                },
            )
            # "nostate" has an ActiveRoom but no RoomCycleState row.
            del engine._room_cycle_states["nostate"]
            # Readings: hot drifted 4°F past target, notemp unreadable.
            temps = {"hot": 74.0, "notemp": None, "nostate": 74.0}
            engine._get_avg_temp = lambda room: temps[room.id]

            drifted = engine._rooms_drifted_past_deadband("cooling", _tc(deadband=0.5))

            assert drifted == ["Sun Room"]
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# Minimum-runtime hold: enter / release (#237, #423)
# ---------------------------------------------------------------------------


class TestMinRuntimeHoldBookkeeping:
    @pytest.mark.asyncio
    async def test_release_is_a_no_op_when_the_cycle_is_not_held(self):
        conn = await _conn()
        try:
            room = await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            ha = _make_ha()
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(room)})
            engine._overflow_room_ids = {"r1"}  # would be closed by a real release

            await engine._release_min_runtime_hold(conn, "no reason")

            assert engine._overflow_room_ids == {"r1"}, "a no-op release must not close vents"
            assert cycle.in_min_runtime_hold is False
            ha.close_cover.assert_not_awaited()

            # ... and with no cycle log at all it is still safe.
            engine._cycle_log = None
            await engine._release_min_runtime_hold(conn, "no reason")
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_release_survives_a_failed_flag_persist(self, caplog):
        """A DB write failure must not strand the cycle in a hold it has
        already left in memory."""
        conn = await _conn()
        try:
            room = await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            overflow = await _add_room(conn, "of", "Guest", vents=["cover.of"])
            ha = _make_ha()
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(room)})
            cycle.in_min_runtime_hold = True
            engine._overflow_room_ids = {overflow.id}
            engine._overflow_room_states = {
                overflow.id: RoomCycleState(
                    cycle_id=cycle.id, room_id=overflow.id, target_temp=72.0, role="overflow"
                )
            }

            with (
                patch.object(db, "set_cycle_log_min_runtime_hold", side_effect=RuntimeError("x")),
                caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER),
            ):
                await engine._release_min_runtime_hold(conn, "a room drifted")

            assert any(
                "Failed to persist in_min_runtime_hold clear" in r.message for r in caplog.records
            ), caplog.text
            assert cycle.in_min_runtime_hold is False
            # The overflow room's vent was still closed and forgotten.
            closed = {c.args[0] for c in ha.close_cover.await_args_list}
            assert "cover.of" in closed
            assert engine._overflow_room_ids == set()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_enter_hold_survives_a_failed_flag_persist(self, caplog):
        conn = await _conn()
        try:
            room = await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            ha = _make_ha()
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(room)})

            with (
                patch.object(db, "set_cycle_log_min_runtime_hold", side_effect=RuntimeError("x")),
                caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER),
            ):
                await engine._enter_min_runtime_hold(conn)

            assert any(
                "Failed to persist in_min_runtime_hold flag" in r.message for r in caplog.records
            ), caplog.text
            assert cycle.in_min_runtime_hold is True, "the in-memory flag still gates this tick"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_enter_hold_reopens_closed_rooms_even_if_diagnostics_writes_fail(self, caplog):
        """The vent-event row is diagnostics only — losing it must never cost
        the re-open that keeps the air handler off a single vent."""
        conn = await _conn()
        try:
            room = await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            ha = _make_ha(cover_state="closed")
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(room)})
            rcs = engine._room_cycle_states["r1"]
            rcs.vent_closed_at = datetime.now(UTC) - timedelta(minutes=5)
            await db.upsert_room_cycle_state(conn, rcs)

            with (
                patch.object(db, "insert_cycle_vent_event", side_effect=RuntimeError("x")),
                caplog.at_level(logging.DEBUG, logger=ENGINE_LOGGER),
            ):
                await engine._enter_min_runtime_hold(conn)

            assert any(
                "Failed to record reopened_min_runtime_hold event" in r.message
                for r in caplog.records
            ), caplog.text
            assert rcs.vent_closed_at is None, "the room must count as open again"
            opened = {c.args[0] for c in ha.open_cover.await_args_list}
            assert opened == {"cover.r1"}
            persisted = await db.get_room_cycle_states(conn, cycle.id)
            assert [p.vent_closed_at for p in persisted] == [None]
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# Overflow conditioning during the hold (#237/#254/#422)
# ---------------------------------------------------------------------------


def _candidate(room: Room, *, tier: int = 1, current: float = 76.0) -> OverflowCandidate:
    return OverflowCandidate(
        room=room,
        current_temp=current,
        effective_setpoint=72.0,
        tier=tier,
        headroom=None,
    )


class TestOverflowRoomClose:
    @pytest.mark.asyncio
    async def test_a_room_with_no_vents_is_skipped(self):
        """An overflow room whose vents were deleted mid-cycle must not blow up
        the close sweep — and must not get a spurious vent event."""
        conn = await _conn()
        try:
            await _add_room(conn, "novents", "Hallway")
            with_vents = await _add_room(conn, "of", "Guest", vents=["cover.of"])
            ha = _make_ha()
            room = await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(room)})
            engine._overflow_room_states = {
                with_vents.id: RoomCycleState(
                    cycle_id=cycle.id, room_id=with_vents.id, target_temp=72.0, role="overflow"
                )
            }

            await engine._close_overflow_rooms(conn, {"novents", with_vents.id}, "done")

            closed = [c.args[0] for c in ha.close_cover.await_args_list]
            assert closed == ["cover.of"]
            events = await db.get_cycle_vent_events(conn, cycle.id)
            assert {e.entity_id for e in events} == {"cover.of"}
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_close_records_the_end_temp_even_if_the_vent_event_write_fails(self, caplog):
        conn = await _conn()
        try:
            overflow = await _add_room(conn, "of", "Guest", vents=["cover.of"])
            room = await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            ha = _make_ha()
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(room)})
            engine._overflow_room_states = {
                overflow.id: RoomCycleState(
                    cycle_id=cycle.id, room_id=overflow.id, target_temp=72.0, role="overflow"
                )
            }
            engine._get_avg_temp = lambda r: 73.5

            with (
                patch.object(db, "insert_cycle_vent_event", side_effect=RuntimeError("x")),
                caplog.at_level(logging.DEBUG, logger=ENGINE_LOGGER),
            ):
                await engine._close_overflow_rooms(conn, {overflow.id}, "no longer a candidate")

            assert any(
                "Failed to record closed_overflow_hold event" in r.message for r in caplog.records
            ), caplog.text
            # The #254 data point still closed.
            assert engine._overflow_room_states[overflow.id].temp_at_end == 73.5
            persisted = {p.room_id: p for p in await db.get_room_cycle_states(conn, cycle.id)}
            assert persisted[overflow.id].temp_at_end == 73.5
        finally:
            await conn.close()


class TestOverflowDuringHold:
    @pytest.mark.asyncio
    async def test_no_active_targets_means_no_candidate_search(self):
        """With no per-room cycle state there is no "active cycle target" to
        judge candidates against, so the algorithm must not run at all."""
        conn = await _conn()
        try:
            room = await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            ha = _make_ha()
            engine, _cycle = await _running_engine(ha, conn, {"r1": _ar(room)})
            engine._room_cycle_states = {}
            picker = AsyncMock(return_value=[])

            with patch.object(room_manager, "get_overflow_candidates", picker):
                await engine._apply_overflow_during_hold(conn, "cooling", _tc())

            picker.assert_not_awaited()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_a_broken_candidate_import_disables_overflow_for_the_tick(
        self, caplog, monkeypatch
    ):
        """The late import is deliberately guarded; if it fails the hold simply
        keeps the active rooms open (pre-#237 behaviour) rather than raising
        out of the tick."""
        conn = await _conn()
        try:
            room = await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            await _add_room(conn, "of", "Guest", vents=["cover.of"])
            ha = _make_ha()
            engine, _cycle = await _running_engine(ha, conn, {"r1": _ar(room)})
            monkeypatch.delattr(room_manager, "get_overflow_candidates")

            with caplog.at_level(logging.DEBUG, logger=ENGINE_LOGGER):
                await engine._apply_overflow_during_hold(conn, "cooling", _tc())

            assert any(
                "Failed to import get_overflow_candidates" in r.message for r in caplog.records
            ), caplog.text
            ha.open_cover.assert_not_awaited()
            ha.close_cover.assert_not_awaited()
            assert engine._overflow_room_ids == set()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_candidate_without_vents_is_skipped_but_still_opens_the_others(self):
        conn = await _conn()
        try:
            room = await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            ventless = await _add_room(conn, "novents", "Hallway")
            usable = await _add_room(conn, "of", "Guest", vents=["cover.of"])
            ha = _make_ha(cover_state="closed")
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(room)})
            picker = AsyncMock(return_value=[_candidate(ventless), _candidate(usable)])

            with patch.object(room_manager, "get_overflow_candidates", picker):
                await engine._apply_overflow_during_hold(conn, "cooling", _tc())

            opened = [c.args[0] for c in ha.open_cover.await_args_list]
            assert opened == ["cover.of"], "the ventless room has nothing to open"
            events = await db.get_cycle_vent_events(conn, cycle.id)
            assert {e.entity_id for e in events} == {"cover.of"}
            # Only the room that actually opened got a #254 data point.
            assert set(engine._overflow_room_states) == {usable.id}
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_open_still_records_the_data_point_when_the_vent_event_write_fails(self, caplog):
        conn = await _conn()
        try:
            room = await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            usable = await _add_room(conn, "of", "Guest", vents=["cover.of"])
            ha = _make_ha(cover_state="closed")
            engine, cycle = await _running_engine(ha, conn, {"r1": _ar(room)})
            picker = AsyncMock(return_value=[_candidate(usable, current=77.5)])

            with (
                patch.object(room_manager, "get_overflow_candidates", picker),
                patch.object(db, "insert_cycle_vent_event", side_effect=RuntimeError("x")),
                caplog.at_level(logging.DEBUG, logger=ENGINE_LOGGER),
            ):
                await engine._apply_overflow_during_hold(conn, "cooling", _tc())

            assert any(
                "Failed to record opened_overflow_hold event" in r.message for r in caplog.records
            ), caplog.text
            opened = [c.args[0] for c in ha.open_cover.await_args_list]
            assert opened == ["cover.of"]
            assert engine._overflow_room_ids == {usable.id}
            persisted = {p.room_id: p for p in await db.get_room_cycle_states(conn, cycle.id)}
            assert persisted[usable.id].temp_at_start == 77.5
            assert persisted[usable.id].role == "overflow"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_record_open_is_a_no_op_without_a_cycle_log(self):
        """``_record_overflow_open`` is keyed on the cycle; with no cycle there
        is nothing to attach the data point to."""
        conn = await _conn()
        try:
            usable = await _add_room(conn, "of", "Guest", vents=["cover.of"])
            engine = _make_engine(_make_ha())

            await engine._record_overflow_open(conn, _candidate(usable), datetime.now(UTC))

            assert engine._overflow_room_states == {}
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_record_open_keeps_in_memory_state_when_the_persist_fails(self, caplog):
        """A failed upsert must not lose the in-memory data point — the cycle-end
        finalize still needs it to close the room out."""
        conn = await _conn()
        try:
            room = await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            usable = await _add_room(conn, "of", "Guest", vents=["cover.of"])
            ha = _make_ha()
            engine, _cycle = await _running_engine(ha, conn, {"r1": _ar(room)})

            with (
                patch.object(db, "upsert_room_cycle_state", side_effect=RuntimeError("x")),
                caplog.at_level(logging.DEBUG, logger=ENGINE_LOGGER),
            ):
                await engine._record_overflow_open(
                    conn, _candidate(usable, current=76.5), datetime.now(UTC)
                )

            assert any(
                "Failed to persist overflow room open state" in r.message for r in caplog.records
            ), caplog.text
            assert engine._overflow_room_states[usable.id].temp_at_start == 76.5
        finally:
            await conn.close()
