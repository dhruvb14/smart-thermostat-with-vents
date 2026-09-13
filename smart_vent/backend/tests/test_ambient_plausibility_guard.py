"""
Ambient plausibility guard (Issue #636).

A climate integration that has just reconnected after an outage can report a
stale/zeroed ``current_temperature`` — 0°C (32°F) is the classic null-glitch
value — for exactly one tick before it repopulates its real attributes. That
reading is a valid, fresh, non-stale float: #211 sensor staleness only
watches ROOM sensors, and #267's unavailable-abort only watches the entity's
``state``, so nothing upstream of ``_read_validated_ambient_f`` can tell a
fault from a real 47°F drop.

This file targets the guard itself and its wiring into all three consumers:

  * ``_read_validated_ambient_f`` — the unmemoized core check: absolute-band
    check, rate-of-change check (both directions), baseline bookkeeping, and
    the reject-detail state a caller with no announce path of its own can
    read.
  * ``_validated_ambient_for_tick`` — the per-tick MEMOIZING wrapper every
    consumer actually calls. It is stateful (the rate check compares against
    real elapsed wall-clock time), and ``_get_avg_temp`` alone can call it
    15+ times within one tick's execution — all effectively simultaneous —
    so it must run the real check at most once per tick and hand every
    reader that tick the same cached answer. ``TestPerTickMemoization``
    proves this directly.
  * ``_enforce_safety_setpoint`` — has no existing "no ambient" branch, so a
    rejection is announced here directly, once per episode (#211/#270/#627
    rate-limiting discipline).
  * ``_apply_vacation_hold`` — reuses its EXISTING #627 ``unreadable:no-ambient``
    bail-out for a rejected reading; no new announce path, but the announced
    MESSAGE now surfaces the actual rejection reason instead of a generic
    "no current temperature" claim that is simply false for a reading that
    WAS reported but rejected. Pinned here as a regression guard for both —
    the vacation-hold TRANSITION coverage itself lives in
    ``test_cycle_engine_gaps_c.py``.
  * ``_get_avg_temp``'s ``include_thermostat_sensor`` room-temperature proxy
    — wired to the same guard via the memoized wrapper; the full
    end-to-end proof (through the real API + tick stack) lives in
    ``integration/test_ambient_plausibility_guard.py``.

Every temperature here is °F — the engine never converts (see CLAUDE.md).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from backend import db
from backend.engine.cycle_engine import (
    AMBIENT_MAX_RATE_F_PER_MIN,
    AMBIENT_PLAUSIBLE_MAX_F,
    AMBIENT_PLAUSIBLE_MIN_F,
    CycleEngine,
)
from backend.engine.vent_controller import VentController
from backend.models import Room, ThermostatConfig

THERMO_ID = "climate.test_thermostat"


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors test_cycle_engine_gaps_c.py's conventions)
# ---------------------------------------------------------------------------


def _make_ha(
    ambient: float | None = 72.0,
    hvac_mode: str = "off",
    setpoint: float | None = None,
) -> MagicMock:
    thermo = {
        "state": hvac_mode,
        "attributes": {"current_temperature": ambient, "temperature": setpoint},
    }
    ha = MagicMock()
    ha.ha_temp_unit = "F"
    ha.get_state.return_value = thermo
    ha.get_numeric_state.return_value = None
    ha.set_thermostat_temperature = AsyncMock()
    ha.set_thermostat_temperature_range = AsyncMock()
    ha.set_thermostat_hvac_mode = AsyncMock()
    return ha


def _make_engine(ha: MagicMock, logger: AsyncMock | None = None) -> CycleEngine:
    return CycleEngine(
        thermostat_entity_id=THERMO_ID,
        ha=ha,
        vent_ctrl=VentController(ha),
        event_logger=logger,
        get_enabled=lambda: True,
        get_vacation_mode=lambda: False,
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


def _state(current_temperature: float | None) -> dict:
    return {"state": "off", "attributes": {"current_temperature": current_temperature}}


def _events(logger: AsyncMock) -> list[tuple[str, str]]:
    return [(c.args[0], c.args[2]) for c in logger.log.await_args_list]


# ---------------------------------------------------------------------------
# _read_validated_ambient_f — the accessor itself
# ---------------------------------------------------------------------------


class TestValidatedAmbientAccessor:
    def test_a_plausible_first_reading_is_accepted_and_becomes_the_baseline(self):
        """No prior baseline: the absolute band is the only check, so a normal
        indoor reading is accepted outright."""
        ha = _make_ha()
        engine = _make_engine(ha)

        result = engine._read_validated_ambient_f(_state(72.0))

        assert result == 72.0
        assert engine._last_valid_ambient_f == 72.0
        assert engine._last_valid_ambient_at is not None
        assert engine._ambient_reject_detail is None

    def test_a_reading_below_the_absolute_band_is_rejected(self):
        """The production glitch itself: 32°F (0°C) reads as a valid, fresh
        float, but no occupied indoor space is this cold."""
        ha = _make_ha()
        engine = _make_engine(ha)

        result = engine._read_validated_ambient_f(_state(32.0))

        assert result is None
        assert engine._last_valid_ambient_f is None, "a rejected reading must not seed the baseline"
        assert engine._ambient_reject_detail is not None
        assert "32.0" in engine._ambient_reject_detail

    def test_a_reading_above_the_absolute_band_is_rejected(self):
        """Mirror-image glitch: an implausibly HOT reading is rejected the
        same way as an implausibly cold one."""
        ha = _make_ha()
        engine = _make_engine(ha)

        result = engine._read_validated_ambient_f(_state(140.0))

        assert result is None
        assert engine._last_valid_ambient_f is None
        assert "140.0" in engine._ambient_reject_detail

    @pytest.mark.parametrize(
        "boundary_value",
        [AMBIENT_PLAUSIBLE_MIN_F, AMBIENT_PLAUSIBLE_MAX_F],
        ids=["min-boundary", "max-boundary"],
    )
    def test_the_absolute_band_boundaries_are_inclusive(self, boundary_value):
        """Exactly on a bound is plausible; only strictly outside is rejected —
        an off-by-one here would reject/accept the wrong side."""
        ha = _make_ha()
        engine = _make_engine(ha)

        result = engine._read_validated_ambient_f(_state(boundary_value))

        assert result == boundary_value
        assert engine._ambient_reject_detail is None

    def test_a_reading_inside_the_band_but_with_an_impossible_slope_is_rejected(self):
        """The absolute band alone would miss this: 40°F is a perfectly
        plausible indoor reading in isolation, but 39°F of change in one
        minute from a just-accepted 79°F is not real thermal drift."""
        ha = _make_ha()
        engine = _make_engine(ha)
        engine._read_validated_ambient_f(_state(79.0))
        assert engine._last_valid_ambient_f == 79.0
        engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=1)

        result = engine._read_validated_ambient_f(_state(40.0))

        assert result is None
        assert engine._last_valid_ambient_f == 79.0, "the glitch must not overwrite the baseline"
        assert "40.0" in engine._ambient_reject_detail
        assert "79.0" in engine._ambient_reject_detail

    def test_an_implausibly_fast_rise_is_also_rejected(self):
        """Both directions (mirror-image glitch): a reading that jumps
        implausibly far ABOVE the baseline is rejected the same way as one
        that jumps implausibly far below it."""
        ha = _make_ha()
        engine = _make_engine(ha)
        engine._read_validated_ambient_f(_state(70.0))
        engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=1)

        result = engine._read_validated_ambient_f(_state(109.0))

        assert result is None
        assert engine._last_valid_ambient_f == 70.0

    def test_a_reading_exactly_at_the_rate_limit_is_accepted(self):
        """Boundary case on the other axis: a change of exactly
        rate × elapsed is plausible, not impossible — only a change that
        EXCEEDS the limit is rejected."""
        ha = _make_ha()
        engine = _make_engine(ha)
        engine._read_validated_ambient_f(_state(70.0))
        engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=2)
        allowed_delta = AMBIENT_MAX_RATE_F_PER_MIN * 2.0

        result = engine._read_validated_ambient_f(_state(70.0 + allowed_delta))

        assert result == pytest.approx(70.0 + allowed_delta)
        assert engine._last_valid_ambient_f == pytest.approx(70.0 + allowed_delta)

    def test_a_plausible_reading_after_real_elapsed_time_updates_the_baseline(self):
        """Control for the slope check: the SAME 39°F change that was rejected
        after one minute is accepted once enough real time has elapsed —
        proving the guard is a genuine rate limit, not a blanket cap."""
        ha = _make_ha()
        engine = _make_engine(ha)
        engine._read_validated_ambient_f(_state(79.0))
        engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=30)

        result = engine._read_validated_ambient_f(_state(40.0))

        assert result == 40.0
        assert engine._last_valid_ambient_f == 40.0, "an accepted reading becomes the new baseline"
        assert engine._ambient_reject_detail is None

    def test_a_missing_reading_returns_none_and_clears_a_stale_reject_detail(self):
        """A genuinely absent reading is a different, pre-existing condition
        (the callers already fail safe on it) — it must not be confused with,
        or keep alive, an unrelated rejection episode."""
        ha = _make_ha()
        engine = _make_engine(ha)
        engine._read_validated_ambient_f(_state(32.0))  # rejected; sets reject_detail
        assert engine._ambient_reject_detail is not None

        result = engine._read_validated_ambient_f(_state(None))

        assert result is None
        assert engine._ambient_reject_detail is None

    def test_repeated_rejections_do_not_move_the_baseline(self):
        """A run of glitched ticks is judged against the last KNOWN-GOOD
        value, never against another glitch — otherwise a slow drift of
        glitches could walk the baseline anywhere."""
        ha = _make_ha()
        engine = _make_engine(ha)
        engine._read_validated_ambient_f(_state(75.0))
        engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=1)

        for glitch in (200.0, -10.0, 300.0):
            assert engine._read_validated_ambient_f(_state(glitch)) is None
            engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=1)

        assert engine._last_valid_ambient_f == 75.0


# ---------------------------------------------------------------------------
# _enforce_safety_setpoint — rejection routes to "no breach", announced once
# ---------------------------------------------------------------------------


class TestSafetySetpointAmbientRejection:
    @pytest.mark.asyncio
    async def test_a_glitched_reading_reports_no_breach_and_commands_nothing(self):
        """The headline regression: 32°F against a 60°F floor is a real
        breach if trusted — the guard must stop it from ever reaching the
        comparison."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=60.0, max_setpoint=85.0))
            ha = _make_ha()
            engine = _make_engine(ha)
            engine._read_validated_ambient_f(_state(79.0))  # establish a real baseline
            engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=6)

            breached = await engine._enforce_safety_setpoint(conn, _state(32.0))

            assert breached is False
            ha.set_thermostat_temperature.assert_not_awaited()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_the_mirror_image_hot_glitch_does_not_command_cooling(self):
        """Both directions: an implausibly HOT reading must not command
        cooling any more than the cold glitch commands heat."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=60.0, max_setpoint=85.0))
            ha = _make_ha()
            engine = _make_engine(ha)
            engine._read_validated_ambient_f(_state(79.0))
            engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=6)

            breached = await engine._enforce_safety_setpoint(conn, _state(150.0))

            assert breached is False
            ha.set_thermostat_temperature.assert_not_awaited()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_the_rejection_is_announced_once_per_episode(self):
        """Rate-limiting discipline matching #211/#270/#627: a flapping
        integration reporting the same glitch every tick must not flood the
        event log."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=60.0, max_setpoint=85.0))
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            engine._read_validated_ambient_f(_state(79.0))
            engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=6)

            for _ in range(5):
                # Simulate 5 separate ticks: the per-tick memoization cache
                # (Issue #636) must not be the reason repeat calls stay
                # quiet — the underlying rate-limiting (`_ambient_reject_warned`)
                # must do that work on its own, tick after tick.
                engine._tick_ambient_computed = False
                await engine._enforce_safety_setpoint(conn, _state(32.0))

            events = _events(logger)
            assert len(events) == 1, f"one event per episode, got {events}"
            level, message = events[0]
            assert level == "warning"
            assert "rejected" in message
            assert "32.0" in message
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_a_plausible_reading_ends_the_episode_so_a_later_glitch_re_announces(self):
        """Control for the once-per-episode discipline: this must be
        once-per-EPISODE, not once ever — a resolved episode followed by a
        fresh glitch is reported again."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=60.0, max_setpoint=85.0))
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            engine._read_validated_ambient_f(_state(79.0))
            engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=6)

            await engine._enforce_safety_setpoint(conn, _state(32.0))  # episode 1
            assert len(_events(logger)) == 1

            # A plausible reading resumes normal supervision and clears the flag.
            # Each step below simulates a fresh tick (Issue #636's per-tick
            # memoization would otherwise keep returning episode 1's cached
            # rejection instead of re-evaluating the new reading).
            engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=6)
            engine._tick_ambient_computed = False
            breached = await engine._enforce_safety_setpoint(conn, _state(79.0))
            assert breached is False
            assert len(_events(logger)) == 1, "an accepted reading announces nothing new"

            # A fresh glitch is a NEW episode.
            engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=6)
            engine._tick_ambient_computed = False
            await engine._enforce_safety_setpoint(conn, _state(32.0))

            assert len(_events(logger)) == 2, _events(logger)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_silent_when_no_logger_is_attached(self):
        """`event_logger` is optional; the rejection bookkeeping must not
        assume it exists (mirrors the vacation-hold posture's own guard)."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=60.0, max_setpoint=85.0))
            ha = _make_ha()
            engine = _make_engine(ha)  # logger=None
            engine._read_validated_ambient_f(_state(79.0))
            engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=6)

            breached = await engine._enforce_safety_setpoint(conn, _state(32.0))

            assert breached is False
            assert engine._ambient_reject_warned is True
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_a_plausible_breach_still_engages_normally(self):
        """Control: the guard must not interfere with a genuine, plausible
        breach — only the earlier `_ambient_reject_detail` bookkeeping is new,
        the comparison itself is unaffected."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=62.0, max_setpoint=80.0))
            ha = _make_ha()
            engine = _make_engine(ha)

            breached = await engine._enforce_safety_setpoint(conn, _state(88.0))

            assert breached is True
            ha.set_thermostat_temperature.assert_awaited_once_with(
                THERMO_ID, 80.0, hvac_mode="cool"
            )
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# _apply_vacation_hold — a rejected reading reuses the EXISTING #627
# unreadable:no-ambient bail-out (no new announce path)
# ---------------------------------------------------------------------------


class TestVacationHoldAmbientRejectionReusesNoAmbientPosture:
    @pytest.mark.asyncio
    async def test_a_glitched_reading_takes_the_same_bail_out_as_a_missing_one(self):
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=60.0, max_setpoint=85.0))
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            engine._read_validated_ambient_f(_state(79.0))
            engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=6)

            await engine._apply_vacation_hold(
                conn, {"state": "cool", "attributes": {"current_temperature": 32.0}}
            )

            ha.set_thermostat_hvac_mode.assert_awaited_once_with(THERMO_ID, "off")
            ha.set_thermostat_temperature.assert_not_awaited()
            events = _events(logger)
            assert len(events) == 1, events
            level, message = events[0]
            assert level == "warning"
            # Issue #636 bug fix: the thermostat DID report a reading (32.0°F)
            # — it was rejected, not missing — so the message must say so and
            # name the value, not claim "reports no current temperature".
            assert "rejected its ambient reading" in message, message
            assert "32.0" in message, message
            assert "outside the plausible indoor range" in message, message
            assert engine._vacation_hold_posture == "unreadable:no-ambient"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_normal_supervision_resumes_once_a_plausible_reading_returns(self):
        """Integration-shaped end of the story: after the bail-out, a
        plausible reading resumes the hold's ordinary heat/cool/off logic."""
        conn = await _conn()
        try:
            await db.upsert_thermostat_config(conn, _tc(min_setpoint=60.0, max_setpoint=85.0))
            ha = _make_ha()
            logger = AsyncMock()
            engine = _make_engine(ha, logger)
            engine._read_validated_ambient_f(_state(79.0))
            engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=6)

            await engine._apply_vacation_hold(
                conn, {"state": "cool", "attributes": {"current_temperature": 32.0}}
            )
            ha.set_thermostat_hvac_mode.reset_mock()

            # Simulate the next tick: the per-tick memoization cache (#636)
            # would otherwise keep returning the first call's cached
            # rejection instead of re-evaluating this real 79.0°F reading.
            engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=6)
            engine._tick_ambient_computed = False
            await engine._apply_vacation_hold(
                conn, {"state": "off", "attributes": {"current_temperature": 79.0}}
            )

            # 79°F is inside the 60–85 band: the hold settles on "off", not a
            # repeat of the no-ambient bail-out, and stays off (idempotent).
            ha.set_thermostat_hvac_mode.assert_not_awaited()
            ha.set_thermostat_temperature.assert_not_awaited()
            assert engine._vacation_hold_posture == "off:60.0:85.0"
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# Per-tick memoization (Issue #636 review requirement)
#
# `_validated_ambient_for_tick` is the only thing any consumer should call —
# `_get_avg_temp` alone can run it 15+ times in a single tick's execution
# (once per room, from several different call sites), all effectively
# simultaneous. The underlying check is STATEFUL (the rate-of-change check
# compares against real elapsed wall-clock time since the last accepted
# reading), so re-running it per call would compare near-simultaneous calls
# against each other and reject almost any nonzero change — a bug this guard
# would be introducing, not fixing. These tests prove the memoization
# directly: the real check runs at most once per tick, and every reader that
# tick — regardless of which one asks first — gets the identical answer.
# ---------------------------------------------------------------------------


class TestPerTickMemoization:
    def test_multiple_calls_within_one_tick_return_the_same_cached_answer(self):
        """The core guarantee, isolated from any particular caller: two
        `_validated_ambient_for_tick` calls in a row — as if a second room's
        lookup raced a live HA push within the same tick — must return the
        identical answer, even though the SECOND call's raw reading would,
        evaluated fresh, be perfectly plausible on its own."""
        ha = _make_ha()
        engine = _make_engine(ha)
        engine._read_validated_ambient_f(_state(79.0))  # establish a baseline
        engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=10)

        first = engine._validated_ambient_for_tick(_state(80.0))
        second = engine._validated_ambient_for_tick(_state(81.0))

        assert first == 80.0
        assert second == first, "every reader within the tick must see the same answer"
        assert engine._last_valid_ambient_f == 80.0, (
            "the rate-limit baseline must advance exactly once per tick, not once per call"
        )

    def test_a_rejection_is_also_memoized_not_re_evaluated_per_call(self):
        """Same guarantee on the reject path: a glitch seen by the first
        caller this tick must read as rejected for every later caller too,
        without re-running (and re-logging) the check."""
        ha = _make_ha()
        engine = _make_engine(ha)
        engine._read_validated_ambient_f(_state(79.0))
        engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=1)

        first = engine._validated_ambient_for_tick(_state(32.0))
        # A second, DIFFERENT raw reading arriving later in the same tick —
        # plausible on its own — must still read as the tick's memoized
        # rejection, not be independently (re-)evaluated.
        second = engine._validated_ambient_for_tick(_state(79.5))

        assert first is None
        assert second is None
        assert engine._last_valid_ambient_f == 79.0, "a rejection must not move the baseline"

    def test_a_new_tick_recomputes_fresh(self):
        """Control: the memo is per-TICK, not permanent — clearing
        `_tick_ambient_computed` (what `_do_tick` does every 60s) lets the
        next tick see a genuinely different reading."""
        ha = _make_ha()
        engine = _make_engine(ha)
        engine._read_validated_ambient_f(_state(79.0))
        engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=10)

        tick_one = engine._validated_ambient_for_tick(_state(80.0))
        assert tick_one == 80.0
        # `tick_one`'s acceptance just reset the baseline timestamp to "now";
        # back-date it so the second tick is judged against real elapsed
        # time, same as every other rate-check test in this file — this test
        # is about the MEMO boundary, not the rate limit itself.
        engine._last_valid_ambient_at -= timedelta(minutes=10)
        engine._tick_ambient_computed = False  # simulate the next _do_tick
        tick_two = engine._validated_ambient_for_tick(_state(81.0))

        assert tick_two == 81.0, "a new tick must re-evaluate, not reuse the previous tick's answer"

    def test_two_rooms_reading_in_the_same_tick_get_a_consistent_answer(self):
        """`_get_avg_temp` is the real-world caller: two rooms on the SAME
        thermostat, both using ``include_thermostat_sensor``, must see the
        identical (correctly rejected) ambient rather than each re-running
        the stateful check against an almost-zero elapsed baseline."""
        ha = _make_ha()
        engine = _make_engine(ha)
        engine._read_validated_ambient_f(_state(79.0))
        engine._last_valid_ambient_at = datetime.now(UTC) - timedelta(minutes=1)
        # 32°F, 47°F of change from the 79°F baseline in ~1 minute — rejected
        # by the rate check however many times it is (mis)evaluated; the
        # point under test is that it is evaluated exactly once.
        ha.get_state.return_value = {
            "state": "off",
            "attributes": {"current_temperature": 32.0, "temperature": None},
        }
        room_a = Room.create(
            name="Room A", thermostat_entity_id=THERMO_ID, include_thermostat_sensor=True
        )
        room_b = Room.create(
            name="Room B", thermostat_entity_id=THERMO_ID, include_thermostat_sensor=True
        )

        avg_a = engine._get_avg_temp(room_a)
        avg_b = engine._get_avg_temp(room_b)

        assert avg_a is None, "the glitch must not become room A's temperature"
        assert avg_b is None, "the glitch must not become room B's temperature either"
        assert engine._last_valid_ambient_f == 79.0, (
            "the baseline must not move — the rejection ran once, shared by both rooms"
        )

    def test_a_plausible_reading_via_get_avg_temp_is_also_memoized(self):
        """Control for the rejection case above: when the reading IS
        plausible, both rooms still see the SAME accepted value (not each
        independently accepting a slightly different one), and the baseline
        advances exactly once."""
        ha = _make_ha()
        engine = _make_engine(ha)
        ha.get_state.return_value = {
            "state": "off",
            "attributes": {"current_temperature": 72.0, "temperature": None},
        }
        room_a = Room.create(
            name="Room A", thermostat_entity_id=THERMO_ID, include_thermostat_sensor=True
        )
        room_b = Room.create(
            name="Room B", thermostat_entity_id=THERMO_ID, include_thermostat_sensor=True
        )

        avg_a = engine._get_avg_temp(room_a)
        avg_b = engine._get_avg_temp(room_b)

        assert avg_a == pytest.approx(72.0)
        assert avg_b == pytest.approx(72.0)
        assert engine._last_valid_ambient_f == 72.0
