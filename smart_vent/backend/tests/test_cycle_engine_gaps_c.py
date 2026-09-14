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

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "rooms_json",
        [
            '["r1"]',
            '{"r1": 74.0}',
            '{"r1": {"name": "Bedroom", "target": "warm", "source": "schedule"}}',
            '{"r1": {"name": "Bedroom", "target": null, "source": "schedule"}}',
            '{"r1": {"name": "Bedroom", "target": 74.0, "requested_target": "n/a"}}',
            # No "target" key at all. Kept out of the old 0.0 default on
            # purpose: restoring this as an active room at 0.0 °F is not a
            # degrade, it is a cooling cycle that can never reach target, so
            # the vent never closes and the timeout monitor ends the cycle.
            '{"r1": {"name": "Bedroom", "source": "schedule"}}',
            # A source that is not a string matches neither "schedule" nor
            # "override", so the #517 deadband re-resolve and the #576
            # respect_eco re-read would both be skipped for the room — and the
            # next in-place trigger update would write the garbage back.
            '{"r1": {"name": "Bedroom", "target": 74.0, "source": {"a": 1}}}',
        ],
        ids=[
            "list-not-object",
            "entry-not-object",
            "target-not-numeric",
            "target-null",
            "requested-target-not-numeric",
            "target-missing",
            "source-not-a-string",
        ],
    )
    async def test_wrong_shaped_rooms_json_restores_a_cycle_with_no_active_rooms(
        self, rooms_json: str
    ):
        """#604: the decode guard above stopped one line too early — the
        snapshot's *shape* and its field *types* were then trusted, so JSON
        that parses fine but is not a dict of per-room dicts with a numeric
        target raised straight out of ``on_startup`` (AttributeError on the
        first two, ValueError/TypeError on the rest). That is a crash loop
        with no web UI to recover from, while the HVAC sits on the last
        setpoint the engine wrote. Each of these must now degrade exactly the
        way an unparseable snapshot already does: the cycle resumes — so the
        timeout monitor and reconciler keep supervising the running HVAC —
        with the unrestorable rooms simply absent.
        """
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            cycle = CycleLog.create(
                thermostat_entity_id=THERMO_ID, mode="cooling", rooms_json=rooms_json
            )
            await db.insert_cycle_log(conn, cycle)
            engine = _make_engine(_make_ha(ambient=76.0))

            await engine.restore_from_db(conn)

            assert engine.cycle_state == CycleState.RUNNING
            assert engine._active_rooms == {}
            assert engine._cycle_log is not None and engine._cycle_log.id == cycle.id
            # The open row is resumed, not discarded — nothing else would close
            # it or supervise the equipment it is still driving.
            assert [c.id for c in await db.get_open_cycle_logs(conn, THERMO_ID)] == [cycle.id]
        finally:
            await conn.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("bad_entry", "expected_warning"),
        [
            ("74.0", "is malformed (float, not an object)"),
            ('{"name": "Bedroom", "target": "warm"}', "non-numeric"),
            ('{"name": "Bedroom"}', "missing or non-numeric"),
            ('{"name": "Bedroom", "target": 74.0, "source": 5}', "has a non-string source"),
        ],
        ids=[
            "entry-not-object",
            "target-not-numeric",
            "target-missing",
            "source-not-a-string",
        ],
    )
    async def test_one_malformed_room_does_not_cost_its_well_formed_siblings(
        self, bad_entry: str, expected_warning: str, caplog
    ):
        """Partial degradation, not all-or-nothing (#604): a snapshot with one
        unreadable entry still restores every entry that *is* readable, and
        names the cycle and the room in the warning so the row can be found."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            await _add_room(conn, "r2", "Office", vents=["cover.r2"])
            cycle = CycleLog.create(
                thermostat_entity_id=THERMO_ID,
                mode="cooling",
                rooms_json=(
                    '{"r1": ' + bad_entry + ", "
                    '"r2": {"name": "Office", "target": 72.0, "source": "schedule"}}'
                ),
            )
            await db.insert_cycle_log(conn, cycle)
            engine = _make_engine(_make_ha(ambient=76.0))

            with caplog.at_level(logging.INFO, logger=ENGINE_LOGGER):
                await engine.restore_from_db(conn)

            assert engine.cycle_state == CycleState.RUNNING
            assert set(engine._active_rooms) == {"r2"}, "the readable sibling must survive"
            assert engine._active_rooms["r2"].target_temp == 72.0
            assert engine._room_vents["r2"] == await db.get_room_vents(conn, "r2")
            logged = [r.getMessage() for r in caplog.records]
            assert any(expected_warning in m and "r1" in m and cycle.id in m for m in logged), (
                logged
            )
            # The skip lands in the existing tally, not swallowed silently.
            assert any("skipped 1 deleted/unrestorable rooms" in m for m in logged), logged
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_a_numeric_requested_target_still_restores_and_arms_eco(self):
        """Control for the type guard above (#604): a snapshot whose
        ``requested_target`` *does* coerce must still restore the eco-relaxed
        target and the ``eco_active`` flag derived from it — the guard must not
        turn a good row into a skipped one."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            await _add_room(conn, "r1", "Bedroom", vents=["cover.r1"])
            cycle = CycleLog.create(
                thermostat_entity_id=THERMO_ID,
                mode="cooling",
                rooms_json=json.dumps(
                    {
                        "r1": {
                            "name": "Bedroom",
                            "target": "76.5",  # strings that coerce are fine
                            "requested_target": 74.0,
                            "source": "schedule",
                        }
                    }
                ),
            )
            await db.insert_cycle_log(conn, cycle)
            engine = _make_engine(_make_ha(ambient=80.0))

            await engine.restore_from_db(conn)

            ar = engine._active_rooms["r1"]
            assert ar.target_temp == 76.5
            assert ar.requested_target == 74.0
            assert ar.eco_active is True
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
        """The idempotence skip compares against the RECOVERY TARGET the hold
        commands (``min_setpoint + deadband``), not the bound that triggered
        it — 62.0 + the 0.5 default deadband (#628)."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            engine = _make_engine(ha)
            state = {
                "state": "heat",
                "attributes": {"current_temperature": 55.0, "temperature": 62.5},
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
                THERMO_ID, 62.5, hvac_mode="heat"
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
                "attributes": {"current_temperature": 88.0, "temperature": 79.5},
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
                THERMO_ID, 79.5, hvac_mode="cool"
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


class TestVacationHoldAnnouncements:
    """The hold's once-per-transition event log (Issue #627).

    Before this the hold commanded heat, cool and off for days at a time and
    wrote exactly one ``event_log`` row in 155 lines — the compressor-lockout
    deferral. From the Logs page a hold that was working and a hold that was
    dead looked identical, which is what made #626 hard to diagnose. The bar
    here is the #212 one: assert the CONSEQUENCE (an event exists, carrying the
    reading and the bound), and assert it does NOT multiply across repeated
    ticks in the same state — a trip is thousands of ticks, and #211/#270
    rate-limit for exactly that reason.
    """

    @staticmethod
    def _events(logger: AsyncMock) -> list[tuple[str, str]]:
        """(level, message) for every event the hold wrote."""
        return [(c.args[0], c.args[2]) for c in logger.log.await_args_list]

    @pytest.mark.asyncio
    async def test_commanding_heat_announces_once_and_not_per_tick(self):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            cold = {"state": "off", "attributes": {"current_temperature": 55.0}}
            # After the command the thermostat reports the hold back; the next
            # ticks of the same episode see it already holding.
            held = {
                "state": "heat",
                "attributes": {"current_temperature": 55.0, "temperature": 62.0},
            }

            await engine._apply_vacation_hold(conn, cold)
            for _ in range(5):
                await engine._apply_vacation_hold(conn, held)

            events = self._events(logger)
            assert len(events) == 1, f"one event per transition, got {events}"
            level, message = events[0]
            assert level == "info"
            assert "55.0°F" in message and "62.0°F" in message, message
            assert "heat" in message.lower(), message
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_commanding_cooling_announces_once_and_not_per_tick(self):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            hot = {"state": "off", "attributes": {"current_temperature": 88.0}}
            held = {
                "state": "cool",
                "attributes": {"current_temperature": 88.0, "temperature": 80.0},
            }

            await engine._apply_vacation_hold(conn, hot)
            for _ in range(5):
                await engine._apply_vacation_hold(conn, held)

            events = self._events(logger)
            assert len(events) == 1, f"one event per transition, got {events}"
            level, message = events[0]
            assert level == "info"
            assert "88.0°F" in message and "80.0°F" in message, message
            assert "cool" in message.lower(), message
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_returning_inside_the_band_announces_off_once_and_not_per_tick(self):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            recovered = {
                "state": "cool",
                "attributes": {"current_temperature": 70.0, "temperature": 80.0},
            }
            settled = {"state": "off", "attributes": {"current_temperature": 70.0}}

            await engine._apply_vacation_hold(conn, recovered)
            for _ in range(5):
                await engine._apply_vacation_hold(conn, settled)

            ha.set_thermostat_hvac_mode.assert_awaited_once_with(THERMO_ID, "off")
            events = self._events(logger)
            assert len(events) == 1, f"one event per transition, got {events}"
            level, message = events[0]
            assert level == "info"
            assert "70.0°F" in message and "62.0°F" in message and "80.0°F" in message, message
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_a_trip_that_starts_inside_the_band_still_says_the_hold_is_alive(self):
        """The hold does not command anything when the thermostat is already
        off inside the band — but an empty Live Feed for a week is the whole
        complaint, so the posture itself is announced."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            idle = {"state": "off", "attributes": {"current_temperature": 70.0}}

            for _ in range(5):
                await engine._apply_vacation_hold(conn, idle)

            ha.set_thermostat_hvac_mode.assert_not_awaited()
            assert len(self._events(logger)) == 1, self._events(logger)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_a_restart_mid_trip_announces_the_hold_it_inherited(self):
        """The engine can come up with the thermostat already executing the
        hold. The idempotence skip (#434/#296) means no command is written —
        the posture is still news, so the feed is not silent for the rest of
        the trip."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            held = {
                "state": "heat",
                "attributes": {"current_temperature": 55.0, "temperature": 62.5},
            }

            await engine._apply_vacation_hold(conn, held)

            ha.set_thermostat_temperature.assert_not_awaited()
            events = self._events(logger)
            assert len(events) == 1 and "heat" in events[0][1].lower(), events
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_each_transition_gets_its_own_event(self):
        """Heat → back in band → cool: three events, in order, and the repeat
        ticks inside each state add nothing."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            sequence = [
                {"state": "off", "attributes": {"current_temperature": 55.0}},
                {"state": "heat", "attributes": {"current_temperature": 58.0, "temperature": 62.0}},
                {"state": "heat", "attributes": {"current_temperature": 70.0, "temperature": 62.0}},
                {"state": "off", "attributes": {"current_temperature": 71.0}},
                {"state": "off", "attributes": {"current_temperature": 88.0}},
                {"state": "cool", "attributes": {"current_temperature": 86.0, "temperature": 80.0}},
            ]

            for state in sequence:
                # Issue #636's plausibility guard rate-limits ambient change;
                # back-date the accepted-reading baseline before each step so
                # this narrative "whole trip" sequence — real jumps spread
                # over the trip, not one instantaneous tick — is not itself
                # read as a glitch. Also reset the per-tick memoization cache,
                # so each step is a fresh tick that actually re-evaluates its
                # own reading instead of reusing an earlier step's answer.
                if engine._last_valid_ambient_at is not None:
                    engine._last_valid_ambient_at -= timedelta(minutes=20)
                engine._ambient_eval_at = None
                await engine._apply_vacation_hold(conn, state)

            messages = [m for _, m in self._events(logger)]
            assert len(messages) == 3, messages
            assert "below min_setpoint" in messages[0], messages[0]
            assert "inside the vacation band" in messages[1], messages[1]
            assert "above max_setpoint" in messages[2], messages[2]
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_editing_the_bound_mid_trip_reannounces_the_new_one(self):
        """The posture is keyed on the bound, not just the mode: raising
        min_setpoint from the Thermostats page while away is a real change to
        what the house is being held at, so it is announced."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            cold = {"state": "off", "attributes": {"current_temperature": 55.0}}

            await engine._apply_vacation_hold(conn, cold)
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=66.0, max_setpoint=80.0))
            await engine._apply_vacation_hold(conn, cold)

            messages = [m for _, m in self._events(logger)]
            assert len(messages) == 2, messages
            assert "62.0°F" in messages[0] and "66.0°F" in messages[1], messages
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_range_mode_announces_the_range_once_across_a_multi_day_trip(self):
        """Acceptance criterion for range mode: safety cycles are structurally
        unavailable there (#626), so the hold is the ONLY mechanism — and it
        used to say nothing at all for the whole trip."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, _tc(vacation_hvac_mode="range", min_setpoint=62.0, max_setpoint=80.0)
            )
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            before = {"state": "off", "attributes": {}}
            holding = {
                "state": "heat_cool",
                "attributes": {"target_temp_low": 62.0, "target_temp_high": 80.0},
            }

            await engine._apply_vacation_hold(conn, before)
            for _ in range(10):  # stand-in for the rest of the trip
                await engine._apply_vacation_hold(conn, holding)

            ha.set_thermostat_temperature_range.assert_awaited_once_with(THERMO_ID, 62.0, 80.0)
            events = self._events(logger)
            assert len(events) == 1, f"one event for the trip, got {events}"
            level, message = events[0]
            assert level == "info"
            assert "62.0°F" in message and "80.0°F" in message, message
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_range_mode_drift_correction_does_not_re_announce(self):
        """External drift makes the hold re-command the identical range. The
        write is worth making; a second identical event is not — a thermostat
        that never accepts the value would otherwise log every 60 s for days."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, _tc(vacation_hvac_mode="range", min_setpoint=62.0, max_setpoint=80.0)
            )
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            drifted = {
                "state": "heat_cool",
                "attributes": {"target_temp_low": 62.0, "target_temp_high": 74.0},
            }

            for _ in range(4):
                await engine._apply_vacation_hold(conn, drifted)

            assert ha.set_thermostat_temperature_range.await_count == 4
            assert len(self._events(logger)) == 1, self._events(logger)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_an_unavailable_thermostat_announces_the_bail_out_once(self):
        """The bail-out that most resembles a dead system: the hold issues no
        commands at all and, before #627, said nothing about it."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)

            for _ in range(5):
                await engine._apply_vacation_hold(conn, {"state": "unavailable", "attributes": {}})
            await engine._apply_vacation_hold(conn, None)

            events = self._events(logger)
            assert len(events) == 1, f"one event per outage, got {events}"
            level, message = events[0]
            assert level == "warning"
            assert "unavailable" in message, message
            ha.set_thermostat_temperature.assert_not_awaited()
            ha.set_thermostat_hvac_mode.assert_not_awaited()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_a_thermostat_with_no_ambient_announces_the_bail_out_once(self):
        """Reachable, but reporting no current temperature: the hold cannot
        compare anything against the band and stops deciding."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc())
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            blind = {"state": "cool", "attributes": {"current_temperature": None}}

            await engine._apply_vacation_hold(conn, blind)
            for _ in range(4):
                await engine._apply_vacation_hold(
                    conn, {"state": "off", "attributes": {"current_temperature": None}}
                )

            events = self._events(logger)
            assert len(events) == 1, f"one event per episode, got {events}"
            level, message = events[0]
            assert level == "warning"
            assert "no ambient reading" in message, message
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_the_ambient_returning_announces_the_hold_again(self):
        """Control for the bail-out above: a reading coming back is a
        transition, so the feed shows the hold resuming."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)

            await engine._apply_vacation_hold(
                conn, {"state": "off", "attributes": {"current_temperature": None}}
            )
            # Issue #636's per-tick memoization caches the first call's answer
            # for the rest of that tick; simulate the SECOND call being a
            # fresh tick so the returning 70.0°F reading is actually
            # re-evaluated rather than reusing the cached "no reading".
            engine._ambient_eval_at = None
            await engine._apply_vacation_hold(
                conn, {"state": "off", "attributes": {"current_temperature": 70.0}}
            )

            levels = [level for level, _ in self._events(logger)]
            assert levels == ["warning", "info"], self._events(logger)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("ambient", "fragment"),
        [(55.0, "could not command heat"), (88.0, "could not command cooling")],
        ids=["heat", "cool"],
    )
    async def test_a_rejected_command_announces_an_error_once_then_stays_quiet(
        self, ambient, fragment, caplog
    ):
        """The failure paths only ever reached the container log, which is not
        where an operator looks. They reach the feed now — once, not on every
        retry, so a thermostat that keeps rejecting the command cannot bury the
        trip under a warning a minute."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            ha.set_thermostat_temperature.side_effect = RuntimeError("HA unreachable")
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            state = {"state": "off", "attributes": {"current_temperature": ambient}}

            with caplog.at_level(logging.ERROR, logger=ENGINE_LOGGER):
                for _ in range(4):
                    await engine._apply_vacation_hold(conn, state)

            assert ha.set_thermostat_temperature.await_count == 4, "it must keep retrying"
            events = self._events(logger)
            assert len(events) == 1, f"one event per failure episode, got {events}"
            level, message = events[0]
            assert level == "error"
            assert fragment in message, message
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_a_command_that_starts_working_announces_the_recovery(self):
        """A rejected command followed by a successful one is two transitions:
        the error must not suppress the success that resolves it."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            ha.set_thermostat_temperature.side_effect = [RuntimeError("HA unreachable"), None]
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            cold = {"state": "off", "attributes": {"current_temperature": 55.0}}

            await engine._apply_vacation_hold(conn, cold)
            await engine._apply_vacation_hold(conn, cold)

            levels = [level for level, _ in self._events(logger)]
            assert levels == ["error", "info"], self._events(logger)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_a_rejected_turn_off_announces_an_error_once(self, caplog):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            ha.set_thermostat_hvac_mode.side_effect = RuntimeError("HA unreachable")
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            state = {
                "state": "cool",
                "attributes": {"current_temperature": 70.0, "temperature": 80.0},
            }

            with caplog.at_level(logging.ERROR, logger=ENGINE_LOGGER):
                for _ in range(3):
                    await engine._apply_vacation_hold(conn, state)

            events = self._events(logger)
            assert len(events) == 1, events
            assert events[0][0] == "error" and "could not turn the HVAC off" in events[0][1]
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_a_rejected_range_write_announces_an_error_once(self, caplog):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, _tc(vacation_hvac_mode="range", min_setpoint=62.0, max_setpoint=80.0)
            )
            ha = _make_ha()
            ha.set_thermostat_temperature_range.side_effect = RuntimeError("HA unreachable")
            logger = AsyncMock()
            engine = _make_engine(ha, logger)

            with caplog.at_level(logging.ERROR, logger=ENGINE_LOGGER):
                for _ in range(3):
                    await engine._apply_vacation_hold(conn, {"state": "off", "attributes": {}})

            events = self._events(logger)
            assert len(events) == 1, events
            assert events[0][0] == "error" and "heat_cool range" in events[0][1]
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_the_compressor_deferral_announces_once_then_the_hold_when_it_elapses(self):
        """The deferral warning predates #627 and fired on EVERY tick of the
        lockout. It is now one event, and the cooling that follows it is
        another — so the feed shows the wait and its end, not a stream of
        identical warnings."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn,
                _tc(min_setpoint=62.0, max_setpoint=80.0, min_cycle_offtime_min=10),
            )
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            engine._last_cycle_ended_at = datetime.now(UTC)
            hot = {"state": "off", "attributes": {"current_temperature": 88.0}}

            for _ in range(5):  # five ticks inside the lockout
                await engine._apply_vacation_hold(conn, hot)
            ha.set_thermostat_temperature.assert_not_awaited()
            assert len(self._events(logger)) == 1, self._events(logger)

            engine._last_cycle_ended_at = datetime.now(UTC) - timedelta(minutes=11)
            await engine._apply_vacation_hold(conn, hot)

            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 79.5, hvac_mode="cool"
            )
            levels = [level for level, _ in self._events(logger)]
            assert levels == ["warning", "info"], self._events(logger)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_the_hold_is_silent_when_no_logger_is_attached(self):
        """`event_logger` is optional; the posture bookkeeping must not assume
        it exists."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            engine = _make_engine(ha)  # logger=None

            await engine._apply_vacation_hold(
                conn, {"state": "off", "attributes": {"current_temperature": 55.0}}
            )

            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 62.5, hvac_mode="heat"
            )
            assert engine._vacation_hold_posture == "heat:62.5"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_leaving_vacation_ends_the_episode_so_the_next_trip_re_announces(self):
        """The posture is per-trip. Without the reset, a second vacation that
        opens in the same state as the first ended would inherit its posture
        and stay silent for its whole duration."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn,
                _tc(min_setpoint=62.0, max_setpoint=80.0, vacation_safety_cycles=False),
            )
            ha = _make_ha(ambient=70.0, hvac_mode="off", setpoint=70.0)
            logger = AsyncMock()
            engine = _make_engine(ha, logger, vacation=True)

            await engine._do_tick(conn)
            await engine._do_tick(conn)  # same trip, same state → no second event
            assert self._events(logger) and len(self._events(logger)) == 1

            engine._get_vacation_mode = lambda: False
            await engine._do_tick(conn)  # home again — episode over
            assert engine._vacation_hold_posture is None

            engine._get_vacation_mode = lambda: True
            await engine._do_tick(conn)

            holds = [m for _, m in self._events(logger) if "Vacation hold" in m]
            assert len(holds) == 2, f"the new trip must announce its own hold, got {holds}"
        finally:
            await conn.close()


class TestVacationHoldHysteresis:
    """The hold recovers to one deadband INSIDE the breached bound (#628).

    Before this it commanded the bound itself, so it heated to exactly
    ``min_setpoint``, arrived, the ``< min_setpoint`` test immediately went
    false, the ``else`` branch shut the HVAC off, the zone drifted back across
    and it started again — edge short-cycling on the one code path that runs
    unattended for days. Production was one tenth of a degree from it:
    ``current_temp 78.0`` against ``max_setpoint 78.0``.

    The trigger stays on the BARE bound; only the target moves. Boundary cases
    are asserted on both sides of both bounds per the #212/#213 bar.
    """

    @staticmethod
    def _commands(ha) -> list[tuple[float, str]]:
        return [
            (c.args[1], c.kwargs["hvac_mode"])
            for c in ha.set_thermostat_temperature.await_args_list
        ]

    @pytest.mark.asyncio
    async def test_cooling_recovers_to_one_deadband_below_the_ceiling(self):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, _tc(min_setpoint=62.0, max_setpoint=80.0, deadband=2.0)
            )
            ha = _make_ha()
            engine = _make_engine(ha)

            await engine._apply_vacation_hold(
                conn, {"state": "off", "attributes": {"current_temperature": 88.0}}
            )

            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 78.0, hvac_mode="cool"
            )
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_heating_recovers_to_one_deadband_above_the_floor(self):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, _tc(min_setpoint=62.0, max_setpoint=80.0, deadband=2.0)
            )
            ha = _make_ha()
            engine = _make_engine(ha)

            await engine._apply_vacation_hold(
                conn, {"state": "off", "attributes": {"current_temperature": 55.0}}
            )

            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 64.0, hvac_mode="heat"
            )
        finally:
            await conn.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("ambient", "expected"),
        [
            (61.9, "heat"),  # just below the floor → trigger
            (62.0, "off"),  # exactly ON the floor is not below it (strict <)
            (79.9, "off"),  # inside the band, near the ceiling
            (80.0, "off"),  # exactly ON the ceiling is not above it (strict >)
            (80.1, "cool"),  # just above the ceiling → trigger
        ],
        ids=["below-floor", "on-floor", "under-ceiling", "on-ceiling", "above-ceiling"],
    )
    async def test_the_trigger_stays_on_the_bare_bound(self, ambient, expected):
        """The inset moves the TARGET, never the trigger: a zone sitting
        exactly on a bound must still be treated as inside the envelope, or the
        fix would widen the band the user configured."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, _tc(min_setpoint=62.0, max_setpoint=80.0, deadband=2.0)
            )
            ha = _make_ha()
            engine = _make_engine(ha)

            await engine._apply_vacation_hold(
                conn, {"state": "heat_cool", "attributes": {"current_temperature": ambient}}
            )

            if expected == "off":
                ha.set_thermostat_temperature.assert_not_awaited()
                ha.set_thermostat_hvac_mode.assert_awaited_once_with(THERMO_ID, "off")
            else:
                ha.set_thermostat_hvac_mode.assert_not_awaited()
                assert self._commands(ha) == [(64.0 if expected == "heat" else 78.0, expected)]
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_arriving_at_the_recovery_target_does_not_re_trigger(self):
        """The anti-flap property itself, stated as a consequence.

        Cool from 88°F; the thermostat reaches the 78°F target. That reading is
        inside the band, so the hold turns off — and re-triggering now takes a
        full 2°F of drift back to 80.1°F rather than the one tenth of a degree
        it used to take.
        """
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, _tc(min_setpoint=62.0, max_setpoint=80.0, deadband=2.0)
            )
            ha = _make_ha()
            engine = _make_engine(ha)

            await engine._apply_vacation_hold(
                conn, {"state": "off", "attributes": {"current_temperature": 88.0}}
            )
            arrived = {
                "state": "cool",
                "attributes": {"current_temperature": 78.0, "temperature": 78.0},
            }
            # Issue #636's plausibility guard rate-limits ambient change;
            # back-date the accepted-reading baseline so "the thermostat
            # reaches its target" reads as the real recovery this test means,
            # not as an implausible jump routed to the unrelated no-ambient
            # bail-out (which would coincidentally also turn the HVAC off).
            # The per-tick memoization cache is reset the same way `_do_tick`
            # would for a fresh tick, so this call actually re-evaluates
            # 78.0°F instead of reusing call 1's cached 88.0°F answer.
            engine._last_valid_ambient_at -= timedelta(minutes=20)
            engine._ambient_eval_at = None
            await engine._apply_vacation_hold(conn, arrived)

            ha.set_thermostat_hvac_mode.assert_awaited_once_with(THERMO_ID, "off")
            # Still one command — arriving did not start a second compressor run.
            assert ha.set_thermostat_temperature.await_count == 1

            # Drifting back up but still inside the band stays off.
            ha.set_thermostat_hvac_mode.reset_mock()
            engine._last_valid_ambient_at -= timedelta(minutes=20)
            engine._ambient_eval_at = None
            await engine._apply_vacation_hold(
                conn, {"state": "off", "attributes": {"current_temperature": 80.0}}
            )
            assert ha.set_thermostat_temperature.await_count == 1
            ha.set_thermostat_hvac_mode.assert_not_awaited()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("ambient", "expected_target", "mode"),
        [(55.0, 74.0, "heat"), (88.0, 70.0, "cool")],
        ids=["heat", "cool"],
    )
    async def test_a_deadband_wider_than_the_band_clamps_to_the_opposite_bound(
        self, ambient, expected_target, mode
    ):
        """Band 70–74 with a 5°F deadband: the raw inset would be 75°F heating
        and 69°F cooling, each PAST the opposite bound, which would hand the
        next tick a breach in the other direction and oscillate heat↔cool.

        Clamped to the opposite bound, exactly as `_add_safety_rooms` clamps
        (#367) — and because the trigger is strict, landing on that bound does
        not re-trigger.
        """
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, _tc(min_setpoint=70.0, max_setpoint=74.0, deadband=5.0)
            )
            ha = _make_ha()
            engine = _make_engine(ha)

            await engine._apply_vacation_hold(
                conn, {"state": "off", "attributes": {"current_temperature": ambient}}
            )

            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, expected_target, hvac_mode=mode
            )
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_range_mode_still_commands_the_bare_bounds(self):
        """Deliberate, not an oversight (#628).

        In heat_cool the EQUIPMENT decides heat vs cool from its own probe and
        applies its own hysteresis, so there is no arrive-and-shut-off edge to
        cushion. Insetting would silently condition the house more tightly than
        the user asked for.
        """
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn,
                _tc(
                    vacation_hvac_mode="range",
                    min_setpoint=62.0,
                    max_setpoint=80.0,
                    deadband=2.0,
                ),
            )
            ha = _make_ha()
            engine = _make_engine(ha)

            await engine._apply_vacation_hold(conn, {"state": "off", "attributes": {}})

            ha.set_thermostat_temperature_range.assert_awaited_once_with(THERMO_ID, 62.0, 80.0)
        finally:
            await conn.close()


class TestVacationHoldLockoutRearm:
    """The compressor off-time lockout re-arms on hold-driven stops (#628).

    `_last_cycle_ended_at` is written only when a CYCLE ends, and the hold
    never creates one. So on a multi-day trip the lockout expired once — after
    the activation abort stamped it — and returned False for the rest of the
    vacation, leaving the hold free to stop and restart the compressor on its
    own bound for days with nobody home. That is the exact hazard the
    #208–#213 wave exists to prevent, and defect 1's flapping is what would
    have driven it.
    """

    @pytest.mark.asyncio
    async def test_the_lockout_applies_to_a_SECOND_hold_driven_start(self):
        """The headline acceptance case, with no cycle involved at any point.

        Cool → recover → the hold turns the compressor off → the zone breaches
        again minutes later. That second start must be deferred.
        """
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn,
                _tc(
                    min_setpoint=62.0,
                    max_setpoint=80.0,
                    deadband=2.0,
                    min_cycle_offtime_min=10,
                    vacation_safety_cycles=False,
                ),
            )
            ha = _make_ha()
            engine = _make_engine(ha)
            assert engine._last_cycle_ended_at is None, "no cycle has ever run in this test"

            # 1. First breach — nothing has stopped a compressor yet, so it runs.
            await engine._apply_vacation_hold(
                conn, {"state": "off", "attributes": {"current_temperature": 88.0}}
            )
            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 78.0, hvac_mode="cool"
            )

            # 2. Recovered into the band — the hold stops the compressor.
            # Issue #636's plausibility guard rate-limits ambient change;
            # back-date the accepted-reading baseline so this (and the
            # breach below) reads as the real multi-minute trip this test
            # narrates, not as an implausible jump routed to the unrelated
            # no-ambient bail-out (which would coincidentally also stop the
            # compressor and satisfy the assertions below for the wrong
            # reason). The per-tick memoization cache is reset the same way
            # `_do_tick` would for each fresh tick, so every step below
            # actually re-evaluates its own reading instead of reusing an
            # earlier call's cached answer.
            engine._last_valid_ambient_at -= timedelta(minutes=20)
            engine._ambient_eval_at = None
            await engine._apply_vacation_hold(
                conn,
                {"state": "cool", "attributes": {"current_temperature": 78.0, "temperature": 78.0}},
            )
            ha.set_thermostat_hvac_mode.assert_awaited_once_with(THERMO_ID, "off")
            assert engine._hold_compressor_off_at is not None, "the stop must arm the lockout"

            # 3. Breaches again straight away — the second start must be deferred.
            ha.set_thermostat_temperature.reset_mock()
            engine._last_valid_ambient_at -= timedelta(minutes=20)
            engine._ambient_eval_at = None
            await engine._apply_vacation_hold(
                conn, {"state": "off", "attributes": {"current_temperature": 88.0}}
            )
            ha.set_thermostat_temperature.assert_not_awaited()

            # 4. Once the off-time has elapsed it starts, as it always could.
            engine._hold_compressor_off_at = datetime.now(UTC) - timedelta(minutes=11)
            engine._ambient_eval_at = None
            await engine._apply_vacation_hold(
                conn, {"state": "off", "attributes": {"current_temperature": 88.0}}
            )
            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 78.0, hvac_mode="cool"
            )
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_losing_the_ambient_reading_also_arms_the_lockout(self):
        """That bail-out turns a running compressor off too."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_cycle_offtime_min=10))
            ha = _make_ha()
            engine = _make_engine(ha)

            await engine._apply_vacation_hold(
                conn, {"state": "cool", "attributes": {"current_temperature": None}}
            )

            ha.set_thermostat_hvac_mode.assert_awaited_once_with(THERMO_ID, "off")
            assert engine._hold_compressor_off_at is not None
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_a_swing_straight_from_cooling_to_heating_arms_the_lockout(self):
        """A 60 s tick can cross the whole band, so cool→heat never passes the
        in-band branch — but the compressor still stopped."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, _tc(min_setpoint=62.0, max_setpoint=80.0, min_cycle_offtime_min=10)
            )
            ha = _make_ha()
            engine = _make_engine(ha)

            await engine._apply_vacation_hold(
                conn,
                {"state": "cool", "attributes": {"current_temperature": 55.0, "temperature": 79.5}},
            )

            assert engine._hold_compressor_off_at is not None
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_stopping_a_heat_cool_thermostat_arms_the_lockout(self):
        """A single-setpoint hold meets a `heat_cool` thermostat when the user
        switches `vacation_hvac_mode` from range to single mid-trip. The
        equipment owned the direction, so we cannot tell whether the compressor
        was running — arm the lockout, which is the protective reading."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, _tc(min_setpoint=62.0, max_setpoint=80.0, min_cycle_offtime_min=10)
            )
            ha = _make_ha()
            engine = _make_engine(ha)

            await engine._apply_vacation_hold(
                conn, {"state": "heat_cool", "attributes": {"current_temperature": 70.0}}
            )

            ha.set_thermostat_hvac_mode.assert_awaited_once_with(THERMO_ID, "off")
            assert engine._hold_compressor_off_at is not None
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_stopping_a_heat_run_does_not_arm_the_compressor_lockout(self):
        """Control, and the reason the stamp is conditional: heating is
        furnace-side and exempt from the lockout throughout the engine. Arming
        it on heat→off would defer a legitimate cooling start for a compressor
        that never ran."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn, _tc(min_setpoint=62.0, max_setpoint=80.0, min_cycle_offtime_min=10)
            )
            ha = _make_ha()
            engine = _make_engine(ha)

            await engine._apply_vacation_hold(
                conn,
                {"state": "heat", "attributes": {"current_temperature": 70.0, "temperature": 64.0}},
            )

            ha.set_thermostat_hvac_mode.assert_awaited_once_with(THERMO_ID, "off")
            assert engine._hold_compressor_off_at is None
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_the_guard_takes_the_LATER_of_the_two_stamps(self):
        """`_compressor_off_since` unifies the cycle stamp and the hold stamp.

        Whichever stopped the compressor most recently is the one the lockout
        must measure from — taking the earlier would under-protect.
        """
        conn = await _conn()
        try:
            tc = _tc(min_cycle_offtime_min=10)
            await db.upsert_thermostat_config(conn, tc)
            engine = _make_engine(_make_ha())
            now = datetime.now(UTC)

            engine._last_cycle_ended_at = now - timedelta(minutes=30)
            engine._hold_compressor_off_at = now - timedelta(minutes=1)
            assert engine._compressor_off_since() == engine._hold_compressor_off_at
            assert engine._in_offtime_lockout(tc) is True

            engine._last_cycle_ended_at = now - timedelta(minutes=1)
            engine._hold_compressor_off_at = now - timedelta(minutes=30)
            assert engine._compressor_off_since() == engine._last_cycle_ended_at
            assert engine._in_offtime_lockout(tc) is True

            # Both long past → clear.
            engine._hold_compressor_off_at = now - timedelta(minutes=30)
            engine._last_cycle_ended_at = now - timedelta(minutes=30)
            assert engine._in_offtime_lockout(tc) is False
            assert engine._offtime_lockout_remaining(tc) == 0.0
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_the_hold_stamp_alone_drives_the_remaining_countdown(self):
        """With no cycle ever run, the remaining-minutes figure the deferral
        warning quotes must come from the hold's own stamp."""
        conn = await _conn()
        try:
            tc = _tc(min_cycle_offtime_min=10)
            await db.upsert_thermostat_config(conn, tc)
            engine = _make_engine(_make_ha())
            engine._hold_compressor_off_at = datetime.now(UTC) - timedelta(minutes=4)

            assert engine._in_offtime_lockout(tc) is True
            assert engine._offtime_lockout_remaining(tc) == pytest.approx(6.0, abs=0.1)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_range_mode_never_arms_the_lockout(self):
        """The accepted #426 gap, pinned so it is a decision rather than a
        surprise: range mode never commands the thermostat off, so there is no
        compressor-stop instant for the hold to stamp. The honest fix for a
        user who needs the protection is single setpoint.
        """
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(
                conn,
                _tc(vacation_hvac_mode="range", min_cycle_offtime_min=10),
            )
            ha = _make_ha()
            engine = _make_engine(ha)

            await engine._apply_vacation_hold(
                conn, {"state": "cool", "attributes": {"current_temperature": 88.0}}
            )

            ha.set_thermostat_temperature_range.assert_awaited_once()
            assert engine._hold_compressor_off_at is None
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


# ---------------------------------------------------------------------------
# _apply_vacation_vent_policy — the #626 "share the air" vent rule
# ---------------------------------------------------------------------------


class TestVacationVentPolicy:
    """Edge arms of the vacation vent policy that the integration suite cannot
    reach cheaply: a non-conditioning mode, a room with no vents at all, a room
    whose vents are already closed, and an engine wired without an event
    logger."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["off", "unknown", ""])
    async def test_non_conditioning_mode_is_a_no_op(self, mode):
        """No air is moving, so there is nothing to share or withhold."""
        conn = await _conn()
        try:
            ha = _make_ha()
            engine = _make_engine(ha, vacation=True)
            engine._get_avg_temp = lambda room: 70.0

            await engine._apply_vacation_vent_policy(conn, _tc(), mode)

            ha.call_service.assert_not_awaited()
            ha.open_cover.assert_not_awaited()
            ha.close_cover.assert_not_awaited()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_rooms_without_vents_are_skipped_on_both_arms(self):
        """A ventless room is inert either way — nothing to open, nothing to
        close — and must not raise on the way past."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=60.0, max_setpoint=85.0))
            # `share` is far from the floor (keep-open arm); `satisfied` is past
            # it (close arm). Neither has a vent row.
            share = Room(id="r-share", name="Share", thermostat_entity_id=THERMO_ID)
            satisfied = Room(id="r-sat", name="Satisfied", thermostat_entity_id=THERMO_ID)
            for room in (share, satisfied):
                await db.upsert_room(conn, room)
            temps = {"r-share": 75.0, "r-sat": 60.0}
            ha = _make_ha()
            engine = _make_engine(ha, vacation=True)
            engine._get_avg_temp = lambda room: temps[room.id]

            await engine._apply_vacation_vent_policy(conn, _tc(), "cooling")

            ha.open_cover.assert_not_awaited()
            ha.close_cover.assert_not_awaited()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_an_already_closed_room_is_not_re_commanded(self):
        """Idempotence, same spirit as #296/#434: a satisfied room whose vent is
        already shut costs no HA call and writes no event."""
        conn = await _conn()
        try:
            tc = _tc(min_setpoint=60.0, max_setpoint=85.0, has_bypass_damper=True)
            await db.upsert_thermostat_config(conn, tc)
            room = Room(id="r-sat", name="Satisfied", thermostat_entity_id=THERMO_ID)
            await db.upsert_room(conn, room)
            await db.add_room_vent(
                conn, RoomVent(id="v1", room_id="r-sat", entity_id="cover.test_vent")
            )
            ha = _make_ha()
            ha.get_state = MagicMock(return_value={"state": "closed", "attributes": {}})
            logger = AsyncMock()
            engine = _make_engine(ha, logger, vacation=True)
            engine._get_avg_temp = lambda room: 60.0  # at the floor → wants closing

            await engine._apply_vacation_vent_policy(conn, tc, "cooling")

            ha.close_cover.assert_not_awaited()
            logger.log.assert_not_awaited()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_closes_without_an_event_logger(self):
        """The engine may be constructed with `event_logger=None`; the close
        must still happen rather than blowing up on the log call."""
        conn = await _conn()
        try:
            tc = _tc(min_setpoint=60.0, max_setpoint=85.0, has_bypass_damper=True)
            await db.upsert_thermostat_config(conn, tc)
            room = Room(id="r-sat", name="Satisfied", thermostat_entity_id=THERMO_ID)
            await db.upsert_room(conn, room)
            await db.add_room_vent(
                conn, RoomVent(id="v1", room_id="r-sat", entity_id="cover.test_vent")
            )
            ha = _make_ha()
            ha.get_state = MagicMock(return_value={"state": "open", "attributes": {}})
            engine = _make_engine(ha, logger=None, vacation=True)
            engine._get_avg_temp = lambda room: 60.0

            await engine._apply_vacation_vent_policy(conn, tc, "cooling")

            ha.close_cover.assert_awaited()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_heating_closes_a_room_nearing_the_ceiling(self):
        """Mirror of the cooling arm: in a heating safety cycle a shared room
        stops absorbing at ``max_setpoint - deadband``."""
        conn = await _conn()
        try:
            tc = _tc(min_setpoint=60.0, max_setpoint=85.0, deadband=2.0, has_bypass_damper=True)
            await db.upsert_thermostat_config(conn, tc)
            warm = Room(id="r-warm", name="Warm", thermostat_entity_id=THERMO_ID)
            cool = Room(id="r-cool", name="Cool", thermostat_entity_id=THERMO_ID)
            for room in (warm, cool):
                await db.upsert_room(conn, room)
            await db.add_room_vent(
                conn, RoomVent(id="v1", room_id="r-warm", entity_id="cover.test_vent")
            )
            await db.add_room_vent(
                conn, RoomVent(id="v2", room_id="r-cool", entity_id="cover.other")
            )
            # Warm is at 83 = 85 − 2 → at its ceiling band, must close.
            # Cool is at 70 → keeps absorbing heat.
            temps = {"r-warm": 83.0, "r-cool": 70.0}
            # Per-entity state so an open() on an already-open vent is correctly
            # skipped (#425) and the assertions below see real commands: the
            # warm vent starts open (so closing it is a real call), the cool one
            # starts closed (so opening it is).
            vent_states = {"cover.test_vent": "open", "cover.other": "closed"}
            ha = _make_ha()
            ha.get_state = MagicMock(
                side_effect=lambda eid: {
                    "state": vent_states.get(eid, "open"),
                    "attributes": {},
                }
            )
            engine = _make_engine(ha, AsyncMock(), vacation=True)
            engine._get_avg_temp = lambda room: temps[room.id]

            await engine._apply_vacation_vent_policy(conn, tc, "heating")

            closed = [c.args[0] for c in ha.close_cover.await_args_list]
            opened = [c.args[0] for c in ha.open_cover.await_args_list]
            assert "cover.test_vent" in closed, f"warm room must stop absorbing; closed={closed}"
            assert "cover.other" in opened, f"cool room keeps sharing; opened={opened}"
            assert "cover.other" not in closed
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_all_safety_rooms_filtered_out_hands_back_to_the_hold(self):
        """Mode filtering can empty the set AFTER `_add_safety_rooms` filled it
        (e.g. the #209 outdoor cooling lockout rules out the only direction the
        breaching rooms want). During vacation the hold must take the thermostat
        back — `_enforce_safety_setpoint` must NOT also run, or the two would
        issue competing setpoint commands on the same tick.
        """
        conn = await _conn()
        try:
            tc = _tc(min_setpoint=60.0, max_setpoint=85.0)
            await db.upsert_thermostat_config(conn, tc)
            room = Room(id="r1", name="Gym", thermostat_entity_id=THERMO_ID)
            await db.upsert_room(conn, room)
            await db.add_room_vent(
                conn, RoomVent(id="v1", room_id="r1", entity_id="cover.test_vent")
            )
            ha = _make_ha()
            engine = _make_engine(ha, AsyncMock(), vacation=True)
            engine._get_avg_temp = lambda room: 90.0  # breaches the 85 ceiling
            engine._filter_rooms_for_mode = AsyncMock(return_value={})
            engine._apply_vacation_hold = AsyncMock()
            engine._enforce_safety_setpoint = AsyncMock()

            await engine._do_tick(conn)

            engine._apply_vacation_hold.assert_awaited()
            engine._enforce_safety_setpoint.assert_not_awaited()
        finally:
            await conn.close()
