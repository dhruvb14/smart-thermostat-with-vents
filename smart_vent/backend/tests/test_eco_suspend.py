"""
Tests for Eco Suspend (Issue #500) — unit level.

Covers:
  - DB helpers: set/get/delete suspensions, replace-on-conflict, the
    delete_thermostat_config cascade, thermostat_config_exists
  - Scheduler: set/clear/get, is_eco_suspended clock semantics, auto-expiry
    sweep, persistence, broadcast payloads
  - Cycle engine: the _apply_eco suspension gate (zone-wide no-op including
    room opt-ins), the RUNNING-cycle snapshot (next-cycle-only), and the
    hysteresis-memory preservation rule
  - Cycle engine: the temporary-hold Eco opt-in (#576) — respect_eco=False
    holds stay never-relaxed (#419), respect_eco=True holds relax like a
    schedule room, and suspension/safety still win
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from backend import db
from backend.engine.cycle_engine import ActiveRoom, CycleEngine, CycleState
from backend.models import Room, ThermostatConfig
from backend.scheduler import Scheduler

THERMO_A = "climate.thermo_a"
THERMO_B = "climate.thermo_b"


async def _setup_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    return conn


def _make_ha() -> MagicMock:
    ha = MagicMock()
    ha.subscribe_all = MagicMock()
    ha.get_state.return_value = None
    ha.dev_mode = False
    ha._dev_logger = None
    return ha


def _make_sched(conn: aiosqlite.Connection) -> Scheduler:
    sched = Scheduler(ha=_make_ha(), db_path=":memory:")
    sched._db_conn = conn
    sched._broadcast = None
    sched._event_logger = None
    sched._engines = {}
    return sched


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_set_get_delete_eco_suspension():
    conn = await _setup_db()
    try:
        assert await db.get_all_eco_suspensions(conn) == {}
        until = datetime.now(UTC) + timedelta(hours=6)
        await db.set_eco_suspension(conn, THERMO_A, until)
        loaded = await db.get_all_eco_suspensions(conn)
        assert set(loaded) == {THERMO_A}
        # Stored naive-UTC, read back UTC-aware, second-level equality.
        assert loaded[THERMO_A] == until.replace(microsecond=until.microsecond)
        assert loaded[THERMO_A].tzinfo is not None

        await db.delete_eco_suspension(conn, THERMO_A)
        assert await db.get_all_eco_suspensions(conn) == {}
        # Deleting a non-existent row is a no-op, not an error.
        await db.delete_eco_suspension(conn, THERMO_A)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_db_set_eco_suspension_replaces_existing():
    conn = await _setup_db()
    try:
        first = datetime.now(UTC) + timedelta(hours=2)
        second = datetime.now(UTC) + timedelta(hours=8)
        await db.set_eco_suspension(conn, THERMO_A, first)
        await db.set_eco_suspension(conn, THERMO_A, second)
        loaded = await db.get_all_eco_suspensions(conn)
        assert loaded[THERMO_A] == second
        assert len(loaded) == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_db_delete_thermostat_config_cascades_suspension():
    conn = await _setup_db()
    try:
        await db.upsert_thermostat_config(conn, ThermostatConfig(thermostat_entity_id=THERMO_A))
        until = datetime.now(UTC) + timedelta(hours=6)
        await db.set_eco_suspension(conn, THERMO_A, until)
        await db.set_eco_suspension(conn, THERMO_B, until)

        await db.delete_thermostat_config(conn, THERMO_A)

        loaded = await db.get_all_eco_suspensions(conn)
        assert THERMO_A not in loaded
        assert THERMO_B in loaded, "other thermostats' suspensions must survive"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_db_thermostat_config_exists():
    conn = await _setup_db()
    try:
        assert not await db.thermostat_config_exists(conn, THERMO_A)
        await db.upsert_thermostat_config(conn, ThermostatConfig(thermostat_entity_id=THERMO_A))
        assert await db.thermostat_config_exists(conn, THERMO_A)
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_set_and_clear_eco_suspend():
    conn = await _setup_db()
    sched = _make_sched(conn)
    try:
        assert sched.get_eco_suspensions() == {}
        assert sched.get_eco_suspend_until(THERMO_A) is None
        assert not sched.is_eco_suspended(THERMO_A)

        until = datetime.now(UTC) + timedelta(hours=6)
        await sched.set_eco_suspend(THERMO_A, until)
        assert sched.is_eco_suspended(THERMO_A)
        assert sched.get_eco_suspend_until(THERMO_A) == until
        # Persisted so a restart reloads it.
        assert THERMO_A in await db.get_all_eco_suspensions(conn)

        await sched.set_eco_suspend(THERMO_A, None)
        assert not sched.is_eco_suspended(THERMO_A)
        assert sched.get_eco_suspend_until(THERMO_A) is None
        assert await db.get_all_eco_suspensions(conn) == {}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_scheduler_is_eco_suspended_expired_entry_not_suspended():
    """An expired row not yet swept must NOT gate — is_eco_suspended compares
    against the clock, not row existence (≤60 s sweep window)."""
    conn = await _setup_db()
    sched = _make_sched(conn)
    try:
        sched._eco_suspends[THERMO_A] = datetime.now(UTC) - timedelta(seconds=1)
        assert not sched.is_eco_suspended(THERMO_A)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_scheduler_eco_suspend_expiry_sweep():
    """The sweep clears only expired suspensions; future ones survive."""
    conn = await _setup_db()
    sched = _make_sched(conn)
    try:
        past = datetime.now(UTC) - timedelta(seconds=1)
        future = datetime.now(UTC) + timedelta(hours=6)
        await sched.set_eco_suspend(THERMO_A, past)
        await sched.set_eco_suspend(THERMO_B, future)

        await sched._check_eco_suspend_expiry()

        assert sched.get_eco_suspend_until(THERMO_A) is None
        assert sched.get_eco_suspend_until(THERMO_B) == future
        loaded = await db.get_all_eco_suspensions(conn)
        assert set(loaded) == {THERMO_B}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_scheduler_eco_suspend_broadcasts_change():
    conn = await _setup_db()
    sched = _make_sched(conn)
    sched._broadcast = AsyncMock()
    try:
        until = datetime.now(UTC) + timedelta(hours=6)
        await sched.set_eco_suspend(THERMO_A, until)
        sched._broadcast.assert_awaited_once_with(
            "eco_suspend_changed",
            {"thermostat_entity_id": THERMO_A, "resume_at": until.isoformat()},
        )
        sched._broadcast.reset_mock()
        await sched.set_eco_suspend(THERMO_A, None)
        sched._broadcast.assert_awaited_once_with(
            "eco_suspend_changed",
            {"thermostat_entity_id": THERMO_A, "resume_at": None},
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_scheduler_set_eco_suspend_does_not_reset_engines():
    """Next-cycle-only (#500): unlike vacation mode, toggling a suspension must
    NOT force-abort or re-tick engines — running cycles finish untouched."""
    conn = await _setup_db()
    sched = _make_sched(conn)
    engine = MagicMock()
    engine.force_abort = AsyncMock()
    sched._engines = {THERMO_A: engine}
    try:
        await sched.set_eco_suspend(THERMO_A, datetime.now(UTC) + timedelta(hours=6))
        await sched.set_eco_suspend(THERMO_A, None)
        engine.force_abort.assert_not_awaited()
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Cycle engine: the _apply_eco gate
# ---------------------------------------------------------------------------

# A degenerate "step" Eco config: any outdoor >= 86 °F relaxes a cooling
# target by the full 4 °F.
_STEP_TC = ThermostatConfig(
    thermostat_entity_id=THERMO_A,
    eco_mode_enabled=True,
    eco_cooling_outdoor_threshold=86.0,
    eco_cooling_full_drift_temp=86.0,
    eco_cooling_max_drift=4.0,
)


def _make_engine(suspended_flag: dict[str, bool]) -> CycleEngine:
    vent_ctrl = MagicMock()
    return CycleEngine(
        thermostat_entity_id=THERMO_A,
        ha=_make_ha(),
        vent_ctrl=vent_ctrl,
        get_enabled=lambda: True,
        get_vacation_mode=lambda: False,
        get_eco_suspended=lambda: suspended_flag["value"],
    )


def _active_room(room: Room, target: float = 70.0) -> ActiveRoom:
    return ActiveRoom(room=room, target_temp=target, source="schedule")


def _room(eco_mode_enabled: bool | None = None) -> Room:
    return Room(
        id="room1",
        name="Room",
        thermostat_entity_id=THERMO_A,
        eco_mode_enabled=eco_mode_enabled,
    )


def test_apply_eco_suspended_is_strict_noop():
    flag = {"value": True}
    engine = _make_engine(flag)
    ar = _active_room(_room())
    engine._apply_eco({"room1": ar}, "cooling", 95.0, _STEP_TC)
    assert ar.target_temp == 70.0, "suspended: target must not be relaxed"
    assert ar.requested_target == 70.0
    assert ar.eco_active is False


def test_apply_eco_not_suspended_relaxes():
    """Control case: same inputs, no suspension → the step config relaxes."""
    flag = {"value": False}
    engine = _make_engine(flag)
    ar = _active_room(_room())
    engine._apply_eco({"room1": ar}, "cooling", 95.0, _STEP_TC)
    assert ar.target_temp == 74.0
    assert ar.requested_target == 70.0
    assert ar.eco_active is True


def test_apply_eco_suspend_wins_over_room_opt_in():
    """Zone-wide rule (#500): a room whose tri-state override explicitly opts
    INTO Eco is still suspended."""
    flag = {"value": True}
    engine = _make_engine(flag)
    tc_off = ThermostatConfig(
        thermostat_entity_id=THERMO_A,
        eco_mode_enabled=False,
        eco_cooling_outdoor_threshold=86.0,
        eco_cooling_full_drift_temp=86.0,
        eco_cooling_max_drift=4.0,
    )
    ar = _active_room(_room(eco_mode_enabled=True))
    engine._apply_eco({"room1": ar}, "cooling", 95.0, tc_off)
    assert ar.target_temp == 70.0
    assert ar.eco_active is False
    # Control: without suspension the room opt-in relaxes despite tc off.
    flag["value"] = False
    ar2 = _active_room(_room(eco_mode_enabled=True))
    engine._apply_eco({"room1": ar2}, "cooling", 95.0, tc_off)
    assert ar2.target_temp == 74.0


def test_apply_eco_running_cycle_uses_snapshot_not_live_flag():
    """Next-cycle-only: a RUNNING cycle keeps the Eco state it started with.
    Suspending mid-cycle must not stop relaxation for rooms joining that
    cycle; only the next fresh start sees the suspension."""
    flag = {"value": False}
    engine = _make_engine(flag)
    # Cycle started un-suspended (IDLE evaluation sampled False)...
    ar = _active_room(_room())
    engine._apply_eco({"room1": ar}, "cooling", 95.0, _STEP_TC)
    assert engine._cycle_eco_suspended is False
    # ...then the user suspends while the cycle runs.
    engine._state = CycleState.RUNNING
    flag["value"] = True
    joiner = _active_room(_room(), target=70.0)
    engine._apply_eco({"room1": joiner}, "cooling", 95.0, _STEP_TC)
    assert joiner.target_temp == 74.0, "running cycle must keep relaxing (snapshot False)"


def test_apply_eco_running_cycle_started_suspended_stays_suspended():
    """Mirror image: a cycle started under a suspension keeps Eco off for its
    whole lifetime even if the suspension is cleared mid-cycle."""
    flag = {"value": True}
    engine = _make_engine(flag)
    ar = _active_room(_room())
    engine._apply_eco({"room1": ar}, "cooling", 95.0, _STEP_TC)
    assert engine._cycle_eco_suspended is True
    engine._state = CycleState.RUNNING
    flag["value"] = False
    joiner = _active_room(_room())
    engine._apply_eco({"room1": joiner}, "cooling", 95.0, _STEP_TC)
    assert joiner.target_temp == 70.0
    assert joiner.eco_active is False


def test_apply_eco_suspension_preserves_hysteresis_memory():
    """A suspension is a pause, not a reset: the engaged-state map survives it
    (same principle as #434's flaky-outdoor-tick rule)."""
    flag = {"value": False}
    engine = _make_engine(flag)
    ar = _active_room(_room())
    engine._apply_eco({"room1": ar}, "cooling", 95.0, _STEP_TC)
    assert engine._eco_engaged.get(("room1", "cooling")) is True

    flag["value"] = True
    ar2 = _active_room(_room())
    engine._apply_eco({"room1": ar2}, "cooling", 95.0, _STEP_TC)
    assert engine._eco_engaged.get(("room1", "cooling")) is True, (
        "suspension must not erase hysteresis engagement"
    )


def test_apply_eco_no_callback_defaults_to_not_suspended():
    """Engines built without the callback (older tests, defensive default)
    behave exactly as before the feature."""
    engine = CycleEngine(
        thermostat_entity_id=THERMO_A,
        ha=_make_ha(),
        vent_ctrl=MagicMock(),
    )
    ar = _active_room(_room())
    engine._apply_eco({"room1": ar}, "cooling", 95.0, _STEP_TC)
    assert ar.target_temp == 74.0
    assert ar.eco_active is True


# ---------------------------------------------------------------------------
# Cycle engine: temporary-hold Eco opt-in (#576)
# ---------------------------------------------------------------------------


def _override_room(respect_eco: bool = False, target: float = 70.0) -> ActiveRoom:
    return ActiveRoom(room=_room(), target_temp=target, source="override", respect_eco=respect_eco)


def test_apply_eco_hold_default_is_never_relaxed():
    """#419 preserved: a hold with the default respect_eco=False is a strict
    no-op — the explicit ask runs unrelaxed and eco_active stays False."""
    flag = {"value": False}
    engine = _make_engine(flag)
    ar = _override_room()
    engine._apply_eco({"room1": ar}, "cooling", 95.0, _STEP_TC)
    assert ar.target_temp == 70.0, "a default hold must never be Eco-relaxed"
    assert ar.requested_target == 70.0
    assert ar.eco_active is False


def test_apply_eco_hold_opt_in_relaxes_like_schedule():
    """respect_eco=True: the hold falls through to the normal relax path and
    is treated exactly like a schedule room (70 → 74 under the step config)."""
    flag = {"value": False}
    engine = _make_engine(flag)
    ar = _override_room(respect_eco=True)
    engine._apply_eco({"room1": ar}, "cooling", 95.0, _STEP_TC)
    assert ar.target_temp == 74.0
    assert ar.requested_target == 70.0
    assert ar.eco_active is True


def test_apply_eco_suspend_wins_over_hold_opt_in():
    """Zone-wide rule (#500): a suspension silences even a hold that opted
    into relaxation — the same strict no-op shape as
    test_apply_eco_suspend_wins_over_room_opt_in."""
    flag = {"value": True}
    engine = _make_engine(flag)
    ar = _override_room(respect_eco=True)
    engine._apply_eco({"room1": ar}, "cooling", 95.0, _STEP_TC)
    assert ar.target_temp == 70.0, "suspension must win over the hold's opt-in"
    assert ar.requested_target == 70.0
    assert ar.eco_active is False


def test_apply_eco_safety_room_ignores_respect_eco():
    """Defensive: respect_eco is an override-only flag. A safety room carrying
    it (nonsensical, but constructible) is still never relaxed — its target is
    a protective recovery bound, not a comfort ask (#409)."""
    flag = {"value": False}
    engine = _make_engine(flag)
    ar = ActiveRoom(room=_room(), target_temp=70.0, source="safety", respect_eco=True)
    engine._apply_eco({"room1": ar}, "cooling", 95.0, _STEP_TC)
    assert ar.target_temp == 70.0, "safety targets are never relaxed, flag or no flag"
    assert ar.requested_target == 70.0
    assert ar.eco_active is False
