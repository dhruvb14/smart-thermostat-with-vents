"""Scheduled vacation start and welcome home (Issue #728), at the scheduler.

The state machine is off → scheduled → active → off, with every transition
running through the scheduler under one lock. This file walks the
(state × event) table from the design (§9), the restart/downtime cases (§4.3)
and the welcome-home tick (§4.7). Engines are mocks: what matters here is which
engines get reset, not what a reset does inside one (the engine-level vacation
hold is covered by test_vacation_mode.py).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from backend import db
from backend.engine import room_manager
from backend.models import PresenceHoldoverState, Room, ThermostatConfig
from backend.scheduler import (
    Scheduler,
    VacationConflict,
    _parse_instant,
    _parse_json_dict,
)

T_A = "climate.a"
T_B = "climate.b"
NOW = datetime(2026, 12, 20, 15, 0, tzinfo=UTC)


def _engine() -> MagicMock:
    eng = MagicMock()
    eng.force_abort = AsyncMock()
    eng.load_room_sensors = AsyncMock()
    eng.tick = AsyncMock()
    return eng


async def _sched(*thermostats: str) -> Scheduler:
    sched = Scheduler(ha=MagicMock(), db_path=":memory:")
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    sched._db_conn = conn
    sched._broadcast = AsyncMock()
    sched._event_logger = MagicMock()
    sched._event_logger.log = AsyncMock()
    sched._engines = {tid: _engine() for tid in thermostats}
    for tid in thermostats:
        await db.upsert_thermostat_config(
            conn, ThermostatConfig(thermostat_entity_id=tid, name=tid.upper(), default_temp=70.0)
        )
    return sched


async def _room(sched: Scheduler, rid: str, tid: str, **kw) -> Room:
    room = Room(id=rid, name=rid, thermostat_entity_id=tid, **kw)
    await db.upsert_room(sched._db_conn, room)
    return room


def _reset_targets(sched: Scheduler) -> set[str]:
    return {tid for tid, eng in sched._engines.items() if eng.force_abort.await_count}


def _clear_resets(sched: Scheduler) -> None:
    for eng in sched._engines.values():
        eng.force_abort.reset_mock()


async def _setting(sched: Scheduler, key: str) -> str:
    return await db.get_system_setting(sched._db_conn, key, "")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def test_parse_instant_handles_empty_naive_and_garbage():
    assert _parse_instant("") is None
    assert _parse_instant("not-a-date") is None
    assert _parse_instant("2026-12-20T15:00:00") == NOW
    assert _parse_instant("2026-12-20T10:00:00-05:00") == NOW


def test_parse_json_dict_rejects_garbage_and_non_objects():
    assert _parse_json_dict("") == {}
    assert _parse_json_dict("{nope") == {}
    assert _parse_json_dict("[1, 2]") == {}
    assert _parse_json_dict('{"a": 1}') == {"a": 1}


# ---------------------------------------------------------------------------
# Scheduling, editing, cancelling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_future_start_schedules_without_touching_engines():
    sched = await _sched(T_A)
    start, ret = NOW + timedelta(days=1), NOW + timedelta(days=8)
    await sched.plan_vacation(ret, start, now=NOW)

    assert sched.get_vacation_state() == "scheduled"
    assert not sched.get_vacation_mode()
    assert sched.get_vacation_start_at() == start
    assert await _setting(sched, "vacation_mode_start_at") == start.isoformat()
    assert await _setting(sched, "vacation_mode_return_at") == ret.isoformat()
    assert await _setting(sched, "vacation_mode_enabled") == "0"
    assert _reset_targets(sched) == set()
    payload = sched._broadcast.await_args.args[1]
    assert payload["state"] == "scheduled"
    assert payload["start_at"] == start.isoformat()
    assert "Vacation scheduled" in sched._event_logger.log.await_args.args[2]
    # The engines still see a normal day.
    assert sched.is_vacation_for(T_A) is False


@pytest.mark.asyncio
async def test_reposting_while_scheduled_is_the_edit():
    sched = await _sched(T_A)
    await sched.plan_vacation(NOW + timedelta(days=8), NOW + timedelta(days=1), now=NOW)
    new_start = NOW + timedelta(days=2)
    await sched.plan_vacation(NOW + timedelta(days=9), new_start, now=NOW)
    assert sched.get_vacation_start_at() == new_start
    assert sched.get_vacation_return_at() == NOW + timedelta(days=9)
    assert "Scheduled vacation edited" in sched._event_logger.log.await_args.args[2]


@pytest.mark.asyncio
async def test_start_now_promotes_a_scheduled_trip():
    sched = await _sched(T_A)
    ret = NOW + timedelta(days=8)
    await sched.plan_vacation(ret, NOW + timedelta(days=1), now=NOW)
    await sched.plan_vacation(ret, None, now=NOW)  # Start now = no start_at
    assert sched.get_vacation_state() == "active"
    assert sched.get_vacation_start_at() is None
    assert await _setting(sched, "vacation_mode_start_at") == ""
    assert _reset_targets(sched) == {T_A}


@pytest.mark.asyncio
async def test_past_start_means_start_now():
    sched = await _sched(T_A)
    await sched.plan_vacation(NOW + timedelta(days=8), NOW - timedelta(hours=1), now=NOW)
    assert sched.get_vacation_state() == "active"


@pytest.mark.asyncio
async def test_future_start_while_active_conflicts():
    sched = await _sched(T_A)
    await sched.plan_vacation(NOW + timedelta(days=8), now=NOW)
    with pytest.raises(VacationConflict):
        await sched.plan_vacation(NOW + timedelta(days=9), NOW + timedelta(days=1), now=NOW)
    assert sched.get_vacation_return_at() == NOW + timedelta(days=8)


@pytest.mark.asyncio
async def test_extending_an_active_trip_does_not_reset_engines():
    """#728 side fix: moving the return no longer force-aborts a running
    vacation safety cycle."""
    sched = await _sched(T_A)
    await sched.plan_vacation(NOW + timedelta(days=8), now=NOW)
    _clear_resets(sched)
    await sched.plan_vacation(NOW + timedelta(days=10), now=NOW)
    assert sched.get_vacation_return_at() == NOW + timedelta(days=10)
    assert _reset_targets(sched) == set()
    assert "return moved" in sched._event_logger.log.await_args.args[2]


@pytest.mark.asyncio
async def test_extending_after_welcome_home_puts_the_thermostat_back_on_hold():
    sched = await _sched(T_A, T_B)
    await sched.plan_vacation(NOW + timedelta(minutes=30), now=NOW)
    await sched._check_welcome_home(now=NOW)  # inside A's and B's 60-min lead
    assert sched.is_vacation_for(T_A) is False
    _clear_resets(sched)
    await sched.plan_vacation(NOW + timedelta(days=3), now=NOW)
    assert sched.is_vacation_for(T_A) is True
    assert await _setting(sched, "vacation_welcome_fired") == ""
    assert _reset_targets(sched) == {T_A, T_B}


@pytest.mark.asyncio
async def test_cancel_a_scheduled_trip():
    sched = await _sched(T_A)
    await sched.plan_vacation(NOW + timedelta(days=8), NOW + timedelta(days=1), now=NOW)
    await sched.set_vacation_mode(False)
    assert sched.get_vacation_state() == "off"
    assert sched.get_vacation_return_at() is None
    assert await _setting(sched, "vacation_mode_start_at") == ""
    assert _reset_targets(sched) == set()
    assert "cancelled" in sched._event_logger.log.await_args.args[2]


@pytest.mark.asyncio
async def test_set_vacation_mode_true_replaces_a_schedule():
    sched = await _sched(T_A)
    await sched.plan_vacation(NOW + timedelta(days=8), NOW + timedelta(days=1), now=NOW)
    await sched.set_vacation_mode(True, NOW + timedelta(days=5))
    assert sched.get_vacation_state() == "active"
    assert sched.get_vacation_start_at() is None


@pytest.mark.asyncio
async def test_disable_while_off_is_harmless():
    sched = await _sched(T_A)
    await sched.set_vacation_mode(False)
    assert sched.get_vacation_state() == "off"


# ---------------------------------------------------------------------------
# The tick: activation, missed trips, expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_before_start_does_nothing():
    sched = await _sched(T_A)
    await sched.plan_vacation(NOW + timedelta(days=8), NOW + timedelta(days=1), now=NOW)
    await sched._check_vacation_start(now=NOW)
    assert sched.get_vacation_state() == "scheduled"


@pytest.mark.asyncio
async def test_tick_with_nothing_scheduled_does_nothing():
    sched = await _sched(T_A)
    await sched._check_vacation_start(now=NOW)
    assert sched.get_vacation_state() == "off"
    await sched.set_vacation_mode(True, NOW + timedelta(days=1))
    await sched._check_vacation_start(now=NOW)  # active: nothing to start
    assert sched.get_vacation_state() == "active"


@pytest.mark.asyncio
async def test_tick_at_start_activates_on_time():
    sched = await _sched(T_A)
    start = NOW + timedelta(days=1)
    await sched.plan_vacation(NOW + timedelta(days=8), start, now=NOW)
    sched._event_logger.log.reset_mock()
    await sched._check_vacation_start(now=start + timedelta(seconds=30))
    assert sched.get_vacation_state() == "active"
    assert _reset_targets(sched) == {T_A}
    messages = [c.args[2] for c in sched._event_logger.log.await_args_list]
    assert any("scheduled start" in m for m in messages)
    assert not any("offline" in m for m in messages)


@pytest.mark.asyncio
async def test_tick_long_after_start_activates_and_says_it_was_late():
    sched = await _sched(T_A)
    start = NOW + timedelta(days=1)
    await sched.plan_vacation(NOW + timedelta(days=8), start, now=NOW)
    await sched._check_vacation_start(now=start + timedelta(hours=5))
    assert sched.get_vacation_state() == "active"
    levels = [c.args[0] for c in sched._event_logger.log.await_args_list]
    assert "warning" in levels


@pytest.mark.asyncio
async def test_whole_trip_missed_goes_off_with_a_notice():
    sched = await _sched(T_A)
    start, ret = NOW + timedelta(days=1), NOW + timedelta(days=2)
    await sched.plan_vacation(ret, start, now=NOW)
    await sched._check_vacation_start(now=ret + timedelta(hours=1))
    assert sched.get_vacation_state() == "off"
    assert _reset_targets(sched) == set()
    missed = {"start_at": start.isoformat(), "return_at": ret.isoformat()}
    assert sched.get_vacation_snapshot()["missed"] == missed
    assert json.loads(await _setting(sched, "vacation_missed_notice")) == missed

    await sched.dismiss_vacation_missed_notice()
    assert sched.get_vacation_snapshot()["missed"] is None
    assert await _setting(sched, "vacation_missed_notice") == ""


@pytest.mark.asyncio
async def test_activation_rechecks_state_under_the_lock():
    """A cancel that wins the lock first leaves nothing for the tick to start."""
    sched = await _sched(T_A)
    start = NOW + timedelta(days=1)
    await sched.plan_vacation(NOW + timedelta(days=8), start, now=NOW)
    async with sched._vacation_lock:
        task = asyncio.ensure_future(sched._check_vacation_start(now=start))
        await asyncio.sleep(0)
        sched._vacation_start_at = None  # the cancel, applied while we hold the lock
        sched._vacation_return_at = None
    await task
    assert sched.get_vacation_state() == "off"
    assert _reset_targets(sched) == set()


@pytest.mark.asyncio
async def test_cancel_racing_activation_leaves_off():
    sched = await _sched(T_A)
    start = NOW + timedelta(days=1)
    await sched.plan_vacation(NOW + timedelta(days=8), start, now=NOW)
    await asyncio.gather(sched._check_vacation_start(now=start), sched.set_vacation_mode(False))
    # Whichever ran first, the end state is off with nothing left scheduled.
    assert sched.get_vacation_state() == "off"
    assert sched.get_vacation_start_at() is None


@pytest.mark.asyncio
async def test_expiry_rechecks_under_the_lock():
    sched = await _sched(T_A)
    await sched.set_vacation_mode(True, NOW + timedelta(hours=1))
    later = NOW + timedelta(hours=2)
    async with sched._vacation_lock:
        task = asyncio.ensure_future(sched._check_vacation_expiry(now=later))
        await asyncio.sleep(0)
        sched._vacation_mode = False  # ended by hand meanwhile
    await task
    assert sched.get_vacation_state() == "off"


@pytest.mark.asyncio
async def test_expiry_resets_only_thermostats_still_on_hold():
    sched = await _sched(T_A, T_B)
    tc_b = await db.get_thermostat_config(sched._db_conn, T_B)
    tc_b.welcome_home_enabled = False
    await db.upsert_thermostat_config(sched._db_conn, tc_b)
    ret = NOW + timedelta(minutes=30)
    await sched.set_vacation_mode(True, ret)
    await sched._check_welcome_home(now=NOW)
    assert set(sched._welcome_fired) == {T_A}
    _clear_resets(sched)
    await sched._check_vacation_expiry(now=ret)
    assert sched.get_vacation_state() == "off"
    # A is mid welcome-home cycle and must not be aborted at the return.
    assert _reset_targets(sched) == {T_B}


@pytest.mark.asyncio
async def test_expiry_before_return_is_a_noop():
    sched = await _sched(T_A)
    await sched.set_vacation_mode(True, NOW + timedelta(hours=1))
    await sched._check_vacation_expiry(now=NOW)
    assert sched.get_vacation_state() == "active"
    await sched.set_vacation_mode(False)
    await sched._check_vacation_expiry(now=NOW)
    assert sched.get_vacation_state() == "off"


@pytest.mark.asyncio
async def test_tick_all_runs_start_then_welcome_then_expiry():
    sched = await _sched()
    order: list[str] = []
    for name in (
        "_check_vacation_start",
        "_check_welcome_home",
        "_check_vacation_expiry",
    ):
        setattr(sched, name, AsyncMock(side_effect=lambda n=name: order.append(n)))
    sched._check_eco_suspend_expiry = AsyncMock()
    sched._check_unit_change = AsyncMock()
    sched._sync_engines = AsyncMock()
    sched._refresh_continuous_presence = AsyncMock()
    await sched._tick_all()
    assert order == ["_check_vacation_start", "_check_welcome_home", "_check_vacation_expiry"]


# ---------------------------------------------------------------------------
# Restart / reload (§4.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_restores_a_scheduled_trip_and_welcome_record():
    sched = await _sched(T_A)
    start, ret = NOW + timedelta(days=1), NOW + timedelta(days=8)
    await sched.plan_vacation(ret, start, now=NOW)
    await db.set_system_setting(
        sched._db_conn, "vacation_welcome_fired", json.dumps({T_A: {"until": "x"}})
    )
    await db.set_system_setting(
        sched._db_conn, "vacation_missed_notice", json.dumps({"start_at": "s", "return_at": "r"})
    )
    fresh = Scheduler(ha=MagicMock(), db_path=":memory:")
    fresh._db_conn = sched._db_conn
    await fresh._load_vacation_state()
    assert fresh.get_vacation_state() == "scheduled"
    assert fresh.get_vacation_start_at() == start
    assert fresh.get_vacation_return_at() == ret
    assert set(fresh._welcome_fired) == {T_A}
    assert fresh._vacation_missed == {"start_at": "s", "return_at": "r"}


@pytest.mark.asyncio
async def test_reload_ignores_a_stray_start_while_active():
    sched = await _sched(T_A)
    conn = sched._db_conn
    await db.set_system_setting(conn, "vacation_mode_enabled", "1")
    await db.set_system_setting(
        conn, "vacation_mode_return_at", (NOW + timedelta(days=1)).isoformat()
    )
    await db.set_system_setting(conn, "vacation_mode_start_at", NOW.isoformat())
    await sched._load_vacation_state()
    assert sched.get_vacation_state() == "active"
    assert sched.get_vacation_start_at() is None


@pytest.mark.asyncio
async def test_pre_728_active_db_keeps_working():
    """A DB from the release before #728: enabled + return_at, no start key."""
    sched = await _sched(T_A)
    conn = sched._db_conn
    ret = NOW + timedelta(hours=2)
    await db.set_system_setting(conn, "vacation_mode_enabled", "1")
    await db.set_system_setting(conn, "vacation_mode_return_at", ret.isoformat())
    await sched._load_vacation_state()
    assert sched.get_vacation_state() == "active"
    await sched._check_welcome_home(now=ret - timedelta(minutes=59))
    assert set(sched._welcome_fired) == {T_A}
    await sched._check_vacation_expiry(now=ret)
    assert sched.get_vacation_state() == "off"


# ---------------------------------------------------------------------------
# Welcome home (§4.7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_welcome_home_writes_presence_for_eligible_rooms_only():
    sched = await _sched(T_A)
    await _room(sched, "plain", T_A)  # presence temp from default_temp
    await _room(sched, "own_temp", T_A, system_wide_temp=68.0)
    await _room(sched, "presence_off", T_A, presence_holdover_hours=0)
    longer = await _room(sched, "longer", T_A)
    long_until = NOW + timedelta(hours=5)
    await db.upsert_holdover_state(
        sched._db_conn,
        PresenceHoldoverState(room_id=longer.id, last_detected_at=NOW, expires_at=long_until),
    )
    ret = NOW + timedelta(minutes=60)
    await sched.set_vacation_mode(True, ret)
    _clear_resets(sched)

    await sched._check_welcome_home(now=NOW - timedelta(minutes=1))  # before lead
    assert sched._welcome_fired == {}

    await sched._check_welcome_home(now=NOW)
    until = NOW + timedelta(hours=1)
    states = {s.room_id: s.expires_at for s in await db.get_all_holdover_states(sched._db_conn)}
    assert states == {"plain": until, "own_temp": until, "longer": long_until}
    assert sched._welcome_fired[T_A] == {"fired_at": NOW.isoformat(), "until": until.isoformat()}
    assert json.loads(await _setting(sched, "vacation_welcome_fired")) == sched._welcome_fired
    assert sched.is_vacation_for(T_A) is False
    assert sched.get_vacation_mode() is True  # the trip itself is still on
    assert _reset_targets(sched) == {T_A}
    assert "3 room(s)" in sched._event_logger.log.await_args.args[2]


@pytest.mark.asyncio
async def test_welcome_home_skips_rooms_with_no_presence_temperature():
    sched = await _sched(T_A)
    tc = await db.get_thermostat_config(sched._db_conn, T_A)
    tc.default_temp = None
    tc.name = ""
    await db.upsert_thermostat_config(sched._db_conn, tc)
    await _room(sched, "no_temp", T_A)
    await sched.set_vacation_mode(True, NOW + timedelta(minutes=10))
    await sched._check_welcome_home(now=NOW)
    assert await db.get_all_holdover_states(sched._db_conn) == []
    # Unnamed thermostat → the log line falls back to its entity id.
    assert T_A in sched._event_logger.log.await_args.args[2]


@pytest.mark.asyncio
async def test_welcome_home_fires_once_per_trip_even_after_restart():
    sched = await _sched(T_A)
    await _room(sched, "r", T_A)
    ret = NOW + timedelta(minutes=30)
    await sched.set_vacation_mode(True, ret)
    await sched._check_welcome_home(now=NOW)
    first = dict(sched._welcome_fired)
    await sched._load_vacation_state()  # restart between the tick and the return
    _clear_resets(sched)
    await sched._check_welcome_home(now=NOW + timedelta(minutes=10))
    assert sched._welcome_fired == first
    assert _reset_targets(sched) == set()


@pytest.mark.asyncio
async def test_welcome_home_honours_disabled_and_lead():
    sched = await _sched(T_A, T_B)
    tc_b = await db.get_thermostat_config(sched._db_conn, T_B)
    tc_b.welcome_home_lead_min = 0  # fires exactly at the return
    await db.upsert_thermostat_config(sched._db_conn, tc_b)
    tc_a = await db.get_thermostat_config(sched._db_conn, T_A)
    tc_a.welcome_home_enabled = False
    await db.upsert_thermostat_config(sched._db_conn, tc_a)
    ret = NOW + timedelta(minutes=30)
    await sched.set_vacation_mode(True, ret)
    await sched._check_welcome_home(now=NOW)
    assert sched._welcome_fired == {}
    await sched._check_welcome_home(now=ret)
    assert set(sched._welcome_fired) == {T_B}


@pytest.mark.asyncio
async def test_two_thermostats_leave_at_their_own_lead_times():
    sched = await _sched(T_A, T_B)
    tc_b = await db.get_thermostat_config(sched._db_conn, T_B)
    tc_b.welcome_home_lead_min = 120
    tc_b.welcome_home_hours = 3.0
    await db.upsert_thermostat_config(sched._db_conn, tc_b)
    ret = NOW + timedelta(hours=2)
    await sched.set_vacation_mode(True, ret)
    await sched._check_welcome_home(now=NOW)
    assert set(sched._welcome_fired) == {T_B}
    assert sched.get_welcome_home_until(now=NOW) == NOW + timedelta(hours=3)
    await sched._check_welcome_home(now=ret - timedelta(minutes=60))
    assert set(sched._welcome_fired) == {T_A, T_B}
    assert sched.get_vacation_snapshot()["welcome_home_until"] is not None


@pytest.mark.asyncio
async def test_welcome_home_needs_an_active_trip_with_a_return():
    sched = await _sched(T_A)
    await sched._check_welcome_home(now=NOW)
    sched._vacation_mode = True  # active with no return time (legacy shape)
    await sched._check_welcome_home(now=NOW)
    assert sched._welcome_fired == {}


@pytest.mark.asyncio
async def test_welcome_home_rechecks_under_the_lock():
    sched = await _sched(T_A)
    await sched.set_vacation_mode(True, NOW + timedelta(minutes=5))
    async with sched._vacation_lock:
        task = asyncio.ensure_future(sched._check_welcome_home(now=NOW))
        await asyncio.sleep(0)
        sched._vacation_mode = False
    await task
    assert sched._welcome_fired == {}


@pytest.mark.asyncio
async def test_ending_early_fires_welcome_home_unless_skipped():
    sched = await _sched(T_A, T_B)
    tc_b = await db.get_thermostat_config(sched._db_conn, T_B)
    tc_b.welcome_home_enabled = False
    await db.upsert_thermostat_config(sched._db_conn, tc_b)
    await _room(sched, "r", T_A)
    await sched.set_vacation_mode(True, NOW + timedelta(days=3))
    await sched.set_vacation_mode(False)
    assert set(sched._welcome_fired) == {T_A}
    assert len(await db.get_all_holdover_states(sched._db_conn)) == 1

    sched2 = await _sched(T_A)
    await _room(sched2, "r", T_A)
    await sched2.set_vacation_mode(True, NOW + timedelta(days=3))
    await sched2.set_vacation_mode(False, welcome_home=False)
    assert sched2._welcome_fired == {}
    assert await db.get_all_holdover_states(sched2._db_conn) == []


@pytest.mark.asyncio
async def test_ending_early_does_not_refire_an_already_fired_thermostat():
    sched = await _sched(T_A)
    await sched.set_vacation_mode(True, NOW + timedelta(minutes=30))
    await sched._check_welcome_home(now=NOW)
    first = dict(sched._welcome_fired)
    await sched.set_vacation_mode(False)
    assert sched._welcome_fired == first


@pytest.mark.asyncio
async def test_next_trip_clears_the_welcome_record():
    sched = await _sched(T_A)
    await sched.set_vacation_mode(True, NOW + timedelta(minutes=30))
    await sched._check_welcome_home(now=NOW)
    await sched.set_vacation_mode(False)
    assert sched._welcome_fired
    await sched.set_vacation_mode(True, NOW + timedelta(days=3))
    assert sched._welcome_fired == {}
    assert sched.is_vacation_for(T_A) is True


@pytest.mark.asyncio
async def test_welcome_until_ignores_closed_and_unreadable_windows():
    sched = await _sched()
    sched._welcome_fired = {
        "x": {"until": (NOW - timedelta(minutes=1)).isoformat()},
        "y": {"until": "garbage"},
        "z": {},
    }
    assert sched.get_welcome_home_until(now=NOW) is None


@pytest.mark.asyncio
async def test_real_presence_never_shortens_the_welcome_window():
    sched = await _sched(T_A)
    room = await _room(sched, "r", T_A, presence_holdover_hours=0.25)
    await sched.set_vacation_mode(True, NOW + timedelta(minutes=30))
    await sched._check_welcome_home(now=NOW)
    await room_manager.handle_presence_event(sched._db_conn, room, now=NOW + timedelta(minutes=5))
    state = await db.get_holdover_state(sched._db_conn, room.id)
    assert state.expires_at == NOW + timedelta(hours=1)
    # Once the window is past, real presence uses the room's own holdover.
    later = NOW + timedelta(hours=2)
    await room_manager.handle_presence_event(sched._db_conn, room, now=later)
    state = await db.get_holdover_state(sched._db_conn, room.id)
    assert state.expires_at == later + timedelta(hours=0.25)


# ---------------------------------------------------------------------------
# Migration (§9): the three thermostat columns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_welcome_home_columns_default_and_round_trip():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    await db.init_db(conn)  # idempotent
    await db.upsert_thermostat_config(conn, ThermostatConfig(thermostat_entity_id=T_A))
    tc = await db.get_thermostat_config(conn, T_A)
    assert (tc.welcome_home_enabled, tc.welcome_home_lead_min, tc.welcome_home_hours) == (
        True,
        60,
        1.0,
    )
    tc.welcome_home_enabled = False
    tc.welcome_home_lead_min = 15
    tc.welcome_home_hours = 2.5
    await db.upsert_thermostat_config(conn, tc)
    tc2 = await db.get_thermostat_config(conn, T_A)
    assert (tc2.welcome_home_enabled, tc2.welcome_home_lead_min, tc2.welcome_home_hours) == (
        False,
        15,
        2.5,
    )
    await conn.close()


def test_row_without_welcome_columns_reads_defaults():
    """An old-schema row (no welcome_home_* keys) reads back with defaults."""

    class _Row(dict):
        def keys(self):
            return list(super().keys())

    base = ThermostatConfig(thermostat_entity_id=T_A)
    row = _Row(
        {
            k: v
            for k, v in base.__dict__.items()
            if not k.startswith("welcome_home") and k != "eco_suspend_until"
        }
    )
    row["vacation_safety_cycles"] = 1
    tc = db._row_to_tc(row)
    assert (tc.welcome_home_enabled, tc.welcome_home_lead_min, tc.welcome_home_hours) == (
        True,
        60,
        1.0,
    )
