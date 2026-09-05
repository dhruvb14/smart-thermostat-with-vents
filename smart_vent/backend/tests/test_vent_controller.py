"""
Tests for the vent controller module.

Covers:
  - open_room_vents: opening vents with state checking
  - close_room_vents: min_open_vents safety guard
  - check_max_closed_duration: force-reopen after timeout
  - _count_open_vents / _is_open helpers
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from backend import db
from backend.engine.vent_controller import VentController
from backend.models import (
    ControlMethod,
    CycleLog,
    Room,
    RoomCycleState,
    RoomVent,
    ThermostatConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

THERMO_ID = "climate.test_thermostat"


def _make_ha(vent_states: dict[str, str] | None = None) -> MagicMock:
    """Mock HAClient with configurable vent states."""
    ha = MagicMock()
    if vent_states is None:
        vent_states = {}

    def get_state(entity_id):
        if entity_id in vent_states:
            return {"state": vent_states[entity_id]}
        return None

    ha.get_state.side_effect = get_state
    ha.open_cover = AsyncMock()
    ha.close_cover = AsyncMock()
    ha.set_cover_position = AsyncMock()
    ha.set_cover_tilt_position = AsyncMock()
    ha.toggle_cover = AsyncMock()
    return ha


class _RecordingLogger:
    """Stand-in for EventLogger — captures calls for assertions."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict | None]] = []

    async def log(
        self, level: str, category: str, message: str, details: dict | None = None
    ) -> None:
        self.events.append((level, category, message, details))


def _make_tc(**overrides: object) -> ThermostatConfig:
    """Build a ThermostatConfig for tests.

    Accepts a legacy ``min_open_vents`` keyword for backwards compatibility
    with tests written against the pre-#213 count-based airflow floor — it is
    translated into the new fields:

    * ``0`` → ``has_bypass_damper=True`` (no airflow floor at all)
    * ``1`` → defaults (``total_vents_count=None`` → engine fallback returns 1)
    * ``N>1`` → ``total_vents_count=N, min_open_vents_fraction=1.0``
      (every smart vent must stay open)
    """
    defaults: dict[str, object] = {
        "thermostat_entity_id": THERMO_ID,
        "max_vent_closed_min": 0,
    }
    legacy = overrides.pop("min_open_vents", None)
    if legacy == 0:
        defaults["has_bypass_damper"] = True
    elif isinstance(legacy, int) and legacy > 1:
        defaults["total_vents_count"] = legacy
        defaults["min_open_vents_fraction"] = 1.0
    defaults.update(overrides)
    return ThermostatConfig(**defaults)  # type: ignore[arg-type]


def _make_vent(
    room_id: str, entity_id: str, control_method: ControlMethod = "open_close"
) -> RoomVent:
    return RoomVent.create(room_id=room_id, entity_id=entity_id, control_method=control_method)


# ---------------------------------------------------------------------------
# open_room_vents
# ---------------------------------------------------------------------------


class TestOpenRoomVents:
    @pytest.mark.asyncio
    async def test_opens_closed_vent(self):
        ha = _make_ha({"cover.vent_1": "closed"})
        ctrl = VentController(ha)
        vent = _make_vent("r1", "cover.vent_1")

        await ctrl.open_room_vents([vent])

        ha.open_cover.assert_called_once_with("cover.vent_1")

    @pytest.mark.asyncio
    async def test_skips_already_open_vent(self):
        ha = _make_ha({"cover.vent_1": "open"})
        ctrl = VentController(ha)
        vent = _make_vent("r1", "cover.vent_1")

        await ctrl.open_room_vents([vent])

        ha.open_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_unknown_entity(self):
        """Vent entity not found in HA → skip (don't crash)."""
        ha = _make_ha({})  # No entities
        ctrl = VentController(ha)
        vent = _make_vent("r1", "cover.vent_missing")

        await ctrl.open_room_vents([vent])

        ha.open_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_opens_multiple_vents(self):
        ha = _make_ha({"cover.v1": "closed", "cover.v2": "closed"})
        ctrl = VentController(ha)
        vents = [_make_vent("r1", "cover.v1"), _make_vent("r1", "cover.v2")]

        await ctrl.open_room_vents(vents)

        assert ha.open_cover.call_count == 2


# ---------------------------------------------------------------------------
# close_room_vents: min_open_vents safety
# ---------------------------------------------------------------------------


class TestCloseRoomVents:
    @pytest.mark.asyncio
    async def test_closes_vent_when_safe(self):
        """Closing one vent still leaves enough open → allowed."""
        ha = _make_ha({"cover.v1": "open", "cover.v2": "open"})
        ctrl = VentController(ha)
        tc = _make_tc(min_open_vents=1)

        room_vents = [_make_vent("r1", "cover.v1")]
        all_vents = [_make_vent("r1", "cover.v1"), _make_vent("r2", "cover.v2")]
        cycle_states: dict[str, RoomCycleState] = {}

        result = await ctrl.close_room_vents(room_vents, all_vents, tc, cycle_states)

        assert result is True
        ha.close_cover.assert_called_once_with("cover.v1")

    @pytest.mark.asyncio
    async def test_defers_when_would_drop_below_min(self):
        """Closing last open vent would drop below min_open_vents → deferred."""
        ha = _make_ha({"cover.v1": "open"})
        ctrl = VentController(ha)
        tc = _make_tc(min_open_vents=1)

        room_vents = [_make_vent("r1", "cover.v1")]
        all_vents = [_make_vent("r1", "cover.v1")]

        result = await ctrl.close_room_vents(room_vents, all_vents, tc, {})

        assert result is False
        ha.close_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_min_open_zero_allows_all_closed(self):
        """min_open_vents=0 → always allow closing."""
        ha = _make_ha({"cover.v1": "open"})
        ctrl = VentController(ha)
        tc = _make_tc(min_open_vents=0)

        room_vents = [_make_vent("r1", "cover.v1")]
        all_vents = [_make_vent("r1", "cover.v1")]

        result = await ctrl.close_room_vents(room_vents, all_vents, tc, {})

        assert result is True
        ha.close_cover.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_already_closed_vent(self):
        """Already-closed vents are not sent close commands."""
        ha = _make_ha({"cover.v1": "closed", "cover.v2": "open"})
        ctrl = VentController(ha)
        tc = _make_tc(min_open_vents=0)

        room_vents = [_make_vent("r1", "cover.v1")]
        all_vents = room_vents

        result = await ctrl.close_room_vents(room_vents, all_vents, tc, {})

        assert result is True
        ha.close_cover.assert_not_called()  # v1 already closed

    @pytest.mark.asyncio
    async def test_min_open_vents_two(self):
        """min_open_vents=2: closing 1 of 3 open → allowed (2 remain)."""
        ha = _make_ha({"cover.v1": "open", "cover.v2": "open", "cover.v3": "open"})
        ctrl = VentController(ha)
        tc = _make_tc(min_open_vents=2)

        room_vents = [_make_vent("r1", "cover.v1")]
        all_vents = [
            _make_vent("r1", "cover.v1"),
            _make_vent("r2", "cover.v2"),
            _make_vent("r3", "cover.v3"),
        ]

        result = await ctrl.close_room_vents(room_vents, all_vents, tc, {})

        assert result is True

    @pytest.mark.asyncio
    async def test_min_open_vents_two_defers(self):
        """min_open_vents=2: closing 1 of 2 open → deferred (would leave 1)."""
        ha = _make_ha({"cover.v1": "open", "cover.v2": "open"})
        ctrl = VentController(ha)
        tc = _make_tc(min_open_vents=2)

        room_vents = [_make_vent("r1", "cover.v1")]
        all_vents = [_make_vent("r1", "cover.v1"), _make_vent("r2", "cover.v2")]

        result = await ctrl.close_room_vents(room_vents, all_vents, tc, {})

        assert result is False


# ---------------------------------------------------------------------------
# check_max_closed_duration
# ---------------------------------------------------------------------------


class TestCheckMaxClosedDuration:
    @pytest.mark.asyncio
    async def test_disabled_when_zero(self):
        """max_vent_closed_min=0 → feature disabled, no reopens."""
        ha = _make_ha({"cover.v1": "closed"})
        ctrl = VentController(ha)
        tc = _make_tc(max_vent_closed_min=0)

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)

        rcs = RoomCycleState(
            cycle_id="c1",
            room_id="r1",
            target_temp=74.0,
            vent_closed_at=datetime(2026, 4, 13, 10, 0),
        )

        result = await ctrl.check_max_closed_duration(
            conn, {"r1": [_make_vent("r1", "cover.v1")]}, {"r1": rcs}, tc
        )
        assert result == []
        ha.open_cover.assert_not_called()
        await conn.close()

    @pytest.mark.asyncio
    async def test_reopens_after_limit(self):
        """Vent closed longer than limit → force-reopen."""
        ha = _make_ha({"cover.v1": "closed"})
        ctrl = VentController(ha)
        tc = _make_tc(max_vent_closed_min=30)

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)

        # Insert room and cycle for FK constraints
        room = Room(id="r1", name="R1", thermostat_entity_id=THERMO_ID)
        await db.upsert_room(conn, room)

        cycle = CycleLog.create(
            thermostat_entity_id=THERMO_ID,
            mode="cooling",
            rooms_json=json.dumps({"r1": {}}),
        )
        cycle.id = "c1"
        await db.insert_cycle_log(conn, cycle)

        closed_at = datetime(2026, 4, 13, 10, 0)
        now = closed_at + timedelta(minutes=35)
        rcs = RoomCycleState(
            cycle_id="c1", room_id="r1", target_temp=74.0, vent_closed_at=closed_at
        )
        await db.upsert_room_cycle_state(conn, rcs)

        result = await ctrl.check_max_closed_duration(
            conn, {"r1": [_make_vent("r1", "cover.v1")]}, {"r1": rcs}, tc, now=now
        )
        assert "r1" in result
        assert rcs.vent_closed_at is None  # timer reset
        await conn.close()

    @pytest.mark.asyncio
    async def test_no_reopen_before_limit(self):
        """Vent closed for less than limit → no action."""
        ha = _make_ha({"cover.v1": "closed"})
        ctrl = VentController(ha)
        tc = _make_tc(max_vent_closed_min=30)

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)

        closed_at = datetime(2026, 4, 13, 10, 0)
        now = closed_at + timedelta(minutes=20)  # only 20 min
        rcs = RoomCycleState(
            cycle_id="c1", room_id="r1", target_temp=74.0, vent_closed_at=closed_at
        )

        result = await ctrl.check_max_closed_duration(
            conn, {"r1": [_make_vent("r1", "cover.v1")]}, {"r1": rcs}, tc, now=now
        )
        assert result == []
        ha.open_cover.assert_not_called()
        await conn.close()

    @pytest.mark.asyncio
    async def test_no_vent_closed_at_skipped(self):
        """Room with vent_closed_at=None → skip (vent is open)."""
        ha = _make_ha({})
        ctrl = VentController(ha)
        tc = _make_tc(max_vent_closed_min=30)

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)

        rcs = RoomCycleState(cycle_id="c1", room_id="r1", target_temp=74.0, vent_closed_at=None)

        result = await ctrl.check_max_closed_duration(
            conn, {"r1": [_make_vent("r1", "cover.v1")]}, {"r1": rcs}, tc
        )
        assert result == []
        await conn.close()


# ---------------------------------------------------------------------------
# Helper method tests
# ---------------------------------------------------------------------------


class TestVentHelpers:
    def test_is_open_true(self):
        ha = _make_ha({"cover.v1": "open"})
        ctrl = VentController(ha)
        assert ctrl._is_open("cover.v1") is True

    def test_is_open_false(self):
        ha = _make_ha({"cover.v1": "closed"})
        ctrl = VentController(ha)
        assert ctrl._is_open("cover.v1") is False

    def test_is_open_unknown_entity(self):
        ha = _make_ha({})
        ctrl = VentController(ha)
        assert ctrl._is_open("cover.missing") is False

    def test_count_open_vents(self):
        ha = _make_ha({"cover.v1": "open", "cover.v2": "closed", "cover.v3": "open"})
        ctrl = VentController(ha)
        vents = [
            _make_vent("r1", "cover.v1"),
            _make_vent("r1", "cover.v2"),
            _make_vent("r2", "cover.v3"),
        ]
        assert ctrl._count_open_vents(vents) == 2

    def test_get_vent_states(self):
        ha = _make_ha({"cover.v1": "open", "cover.v2": "closed"})
        ctrl = VentController(ha)
        vents = [_make_vent("r1", "cover.v1"), _make_vent("r1", "cover.v2")]

        states = ctrl.get_vent_states(vents)
        assert states["cover.v1"] == "open"
        assert states["cover.v2"] == "closed"

    def test_get_vent_states_unknown(self):
        ha = _make_ha({})
        ctrl = VentController(ha)
        vents = [_make_vent("r1", "cover.missing")]

        states = ctrl.get_vent_states(vents)
        assert states["cover.missing"] == "unknown"


# ---------------------------------------------------------------------------
# Per-vent control_method dispatch
# ---------------------------------------------------------------------------


class TestControlMethodDispatch:
    @pytest.mark.asyncio
    async def test_open_uses_open_cover_by_default(self):
        ha = _make_ha({"cover.v1": "closed"})
        ctrl = VentController(ha)
        vent = _make_vent("r1", "cover.v1", control_method="open_close")

        await ctrl.open_room_vents([vent])

        ha.open_cover.assert_called_once_with("cover.v1")
        ha.set_cover_position.assert_not_called()
        ha.set_cover_tilt_position.assert_not_called()
        ha.toggle_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_open_uses_set_position(self):
        ha = _make_ha({"cover.v1": "closed"})
        ctrl = VentController(ha)
        vent = _make_vent("r1", "cover.v1", control_method="set_position")

        await ctrl.open_room_vents([vent])

        ha.set_cover_position.assert_called_once_with("cover.v1", 100)
        ha.open_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_open_uses_set_tilt_position(self):
        ha = _make_ha({"cover.v1": "closed"})
        ctrl = VentController(ha)
        vent = _make_vent("r1", "cover.v1", control_method="set_tilt_position")

        await ctrl.open_room_vents([vent])

        ha.set_cover_tilt_position.assert_called_once_with("cover.v1", 100)
        ha.open_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_open_uses_toggle(self):
        ha = _make_ha({"cover.v1": "closed"})
        ctrl = VentController(ha)
        vent = _make_vent("r1", "cover.v1", control_method="toggle")

        await ctrl.open_room_vents([vent])

        ha.toggle_cover.assert_called_once_with("cover.v1")
        ha.open_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_uses_close_cover_by_default(self):
        ha = _make_ha({"cover.v1": "open"})
        ctrl = VentController(ha)
        tc = _make_tc(min_open_vents=0)
        vent = _make_vent("r1", "cover.v1", control_method="open_close")

        await ctrl.close_room_vents([vent], [vent], tc, {})

        ha.close_cover.assert_called_once_with("cover.v1")

    @pytest.mark.asyncio
    async def test_close_uses_set_position_zero(self):
        ha = _make_ha({"cover.v1": "open"})
        ctrl = VentController(ha)
        tc = _make_tc(min_open_vents=0)
        vent = _make_vent("r1", "cover.v1", control_method="set_position")

        await ctrl.close_room_vents([vent], [vent], tc, {})

        ha.set_cover_position.assert_called_once_with("cover.v1", 0)
        ha.close_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_uses_set_tilt_position_zero(self):
        ha = _make_ha({"cover.v1": "open"})
        ctrl = VentController(ha)
        tc = _make_tc(min_open_vents=0)
        vent = _make_vent("r1", "cover.v1", control_method="set_tilt_position")

        await ctrl.close_room_vents([vent], [vent], tc, {})

        ha.set_cover_tilt_position.assert_called_once_with("cover.v1", 0)
        ha.close_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_uses_toggle(self):
        ha = _make_ha({"cover.v1": "open"})
        ctrl = VentController(ha)
        tc = _make_tc(min_open_vents=0)
        vent = _make_vent("r1", "cover.v1", control_method="toggle")

        await ctrl.close_room_vents([vent], [vent], tc, {})

        ha.toggle_cover.assert_called_once_with("cover.v1")
        ha.close_cover.assert_not_called()


# ---------------------------------------------------------------------------
# Service-call failure handling — never abort the cycle, always log
# ---------------------------------------------------------------------------


class TestVentFailureLogging:
    @pytest.mark.asyncio
    async def test_open_failure_logs_and_continues(self):
        """If one vent raises during open, the remaining vents still open."""
        ha = _make_ha({"cover.v1": "closed", "cover.v2": "closed"})
        ha.open_cover.side_effect = [RuntimeError("HA boom"), None]
        event_logger = _RecordingLogger()
        ctrl = VentController(ha, event_logger=event_logger)

        vents = [_make_vent("r1", "cover.v1"), _make_vent("r1", "cover.v2")]
        # Must not raise — failures are swallowed per vent
        await ctrl.open_room_vents(vents)

        assert ha.open_cover.call_count == 2
        # One error event for v1, one info event for v2 (successful open)
        errors = [e for e in event_logger.events if e[0] == "error"]
        assert len(errors) == 1
        assert "cover.v1" in errors[0][2]
        assert "open" in errors[0][2].lower()

    @pytest.mark.asyncio
    async def test_close_failure_logs_and_continues(self):
        """If one vent raises during close, the remaining vents still close."""
        ha = _make_ha({"cover.v1": "open", "cover.v2": "open"})
        ha.close_cover.side_effect = [RuntimeError("nope"), None]
        event_logger = _RecordingLogger()
        ctrl = VentController(ha, event_logger=event_logger)
        tc = _make_tc(min_open_vents=0)

        vents = [_make_vent("r1", "cover.v1"), _make_vent("r1", "cover.v2")]
        result = await ctrl.close_room_vents(vents, vents, tc, {})

        # Cycle does not abort — close_room_vents returns True (close attempted)
        assert result is True
        assert ha.close_cover.call_count == 2
        errors = [e for e in event_logger.events if e[0] == "error"]
        assert len(errors) == 1
        assert "cover.v1" in errors[0][2]

    @pytest.mark.asyncio
    async def test_set_tilt_failure_logs_method_in_details(self):
        """Failure details include control_method so users can see which was tried."""
        ha = _make_ha({"cover.v1": "closed"})
        ha.set_cover_tilt_position.side_effect = RuntimeError("unsupported service")
        event_logger = _RecordingLogger()
        ctrl = VentController(ha, event_logger=event_logger)

        vent = _make_vent("r1", "cover.v1", control_method="set_tilt_position")
        await ctrl.open_room_vents([vent])

        errors = [e for e in event_logger.events if e[0] == "error"]
        assert len(errors) == 1
        _, category, message, details = errors[0]
        assert category == "engine"
        assert "set_tilt_position" in message
        assert details is not None
        assert details["control_method"] == "set_tilt_position"
        assert details["direction"] == "open"

    @pytest.mark.asyncio
    async def test_close_all_zone_vents_continues_on_failure(self):
        """Emergency close: one failure must not prevent the rest from closing."""
        ha = _make_ha({"cover.v1": "open", "cover.v2": "open"})
        ha.close_cover.side_effect = [RuntimeError("boom"), None]
        event_logger = _RecordingLogger()
        ctrl = VentController(ha, event_logger=event_logger)

        vents = [_make_vent("r1", "cover.v1"), _make_vent("r1", "cover.v2")]
        await ctrl.close_all_zone_vents(vents)

        assert ha.close_cover.call_count == 2
        errors = [e for e in event_logger.events if e[0] == "error"]
        assert len(errors) == 1


# ---------------------------------------------------------------------------
# required_open_vents — airflow-floor helper (Issue #213)
# ---------------------------------------------------------------------------


class TestRequiredOpenVents:
    """The fraction-of-total airflow-floor calculation.

    Pre-#213 the engine used a flat ``min_open_vents`` count which didn't know
    about passive (non-smart) vents or bypass dampers. ``required_open_vents``
    is the per-tick translation of the new ThermostatConfig fields into the
    same simple integer the close-path compares against ``open - would_close``.
    """

    def test_bypass_damper_disables_the_floor(self):
        from backend.engine.vent_controller import required_open_vents

        tc = _make_tc(has_bypass_damper=True, total_vents_count=12)
        # Bypass damper relieves duct pressure mechanically — the airflow floor
        # is not enforced even with total_vents_count set.
        assert required_open_vents(tc, total_smart_vents=4) == 0

    def test_unconfigured_thermostat_falls_back_to_one(self):
        from backend.engine.vent_controller import required_open_vents

        # Pre-#213 thermostat: total_vents_count is still NULL.  The engine
        # falls back to the prior ``min_open_vents=1`` default so safety does
        # not silently lapse during the upgrade window. The Thermostats-page
        # banner asks the user to fill the field in.
        tc = _make_tc()  # total_vents_count defaults to None
        assert tc.total_vents_count is None
        assert required_open_vents(tc, total_smart_vents=3) == 1

    def test_fraction_with_passive_vents_allows_more_closed(self):
        from backend.engine.vent_controller import required_open_vents

        # 12 total / 4 smart / 1/3 fraction: 8 passive vents are always open,
        # ceil(12 * 1/3) = 4 must stay open total → 4 − 8 → clamped to 0.  All
        # four smart vents may close.
        tc = _make_tc(total_vents_count=12, min_open_vents_fraction=1.0 / 3.0)
        assert required_open_vents(tc, total_smart_vents=4) == 0

    def test_fraction_with_no_passive_vents_enforces_floor(self):
        from backend.engine.vent_controller import required_open_vents

        # 4 total / 4 smart / 1/3 → ceil(4 * 1/3) = 2 smart vents must stay open.
        tc = _make_tc(total_vents_count=4, min_open_vents_fraction=1.0 / 3.0)
        assert required_open_vents(tc, total_smart_vents=4) == 2

    def test_ceiling_rounding(self):
        from backend.engine.vent_controller import required_open_vents

        # 7 * 0.5 = 3.5 → ceil → 4. With 7 smart vents, 4 must stay open.
        tc = _make_tc(total_vents_count=7, min_open_vents_fraction=0.5)
        assert required_open_vents(tc, total_smart_vents=7) == 4

    def test_floor_never_negative_when_smart_exceeds_total(self):
        from backend.engine.vent_controller import required_open_vents

        # Misconfiguration: user claims 2 total but we already know about 5
        # smart vents.  ``always_open_passive`` clamps at 0 instead of going
        # negative, so the result stays sane: ceil(2 * 0.5) - 0 = 1.
        tc = _make_tc(total_vents_count=2, min_open_vents_fraction=0.5)
        assert required_open_vents(tc, total_smart_vents=5) == 1

    def test_full_fraction_pins_every_smart_vent_open(self):
        from backend.engine.vent_controller import required_open_vents

        # fraction=1.0 with total=smart=3 → every smart vent must stay open.
        # Used by the test-helper's ``min_open_vents=N`` legacy translation.
        tc = _make_tc(total_vents_count=3, min_open_vents_fraction=1.0)
        assert required_open_vents(tc, total_smart_vents=3) == 3


# ---------------------------------------------------------------------------
# Issue #425: _is_open / _is_fully_open are control-method-aware
# ---------------------------------------------------------------------------


def _make_ha_with_states(states: dict[str, dict]) -> MagicMock:
    """Mock HAClient whose get_state returns full state dicts (attributes)."""
    ha = MagicMock()
    ha.get_state.side_effect = lambda eid: states.get(eid)
    ha.open_cover = AsyncMock()
    ha.close_cover = AsyncMock()
    ha.set_cover_position = AsyncMock()
    ha.set_cover_tilt_position = AsyncMock()
    ha.toggle_cover = AsyncMock()
    return ha


class TestMethodAwareIsOpen:
    """HA derives cover `state` from position, not tilt — a tilt vent can
    report state="open" while tilt-closed. _is_open must judge airflow per
    control method (#425)."""

    def test_tilt_vent_state_open_but_tilt_closed_is_not_open(self):
        ha = _make_ha_with_states(
            {"cover.v": {"state": "open", "attributes": {"current_tilt_position": 0}}}
        )
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="set_tilt_position")
        assert vc._is_open(vent) is False

    def test_tilt_vent_with_tilt_open_is_open_even_if_state_closed(self):
        ha = _make_ha_with_states(
            {"cover.v": {"state": "closed", "attributes": {"current_tilt_position": 100}}}
        )
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="set_tilt_position")
        assert vc._is_open(vent) is True

    def test_position_vent_partially_open_counts_as_passing_air(self):
        ha = _make_ha_with_states(
            {"cover.v": {"state": "open", "attributes": {"current_position": 50}}}
        )
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="set_position")
        assert vc._is_open(vent) is True

    def test_missing_attribute_falls_back_to_state(self):
        ha = _make_ha_with_states({"cover.v": {"state": "open", "attributes": {}}})
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="set_tilt_position")
        assert vc._is_open(vent) is True

    def test_bare_entity_id_keeps_legacy_state_check(self):
        ha = _make_ha_with_states(
            {"cover.v": {"state": "open", "attributes": {"current_tilt_position": 0}}}
        )
        vc = VentController(ha)
        assert vc._is_open("cover.v") is True  # method unknown → state only

    def test_open_close_vent_unchanged(self):
        ha = _make_ha_with_states({"cover.v": {"state": "closed", "attributes": {}}})
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="open_close")
        assert vc._is_open(vent) is False

    def test_count_open_vents_is_method_aware(self):
        ha = _make_ha_with_states(
            {
                "cover.tilt": {"state": "open", "attributes": {"current_tilt_position": 0}},
                "cover.plain": {"state": "open", "attributes": {}},
            }
        )
        vc = VentController(ha)
        vents = [
            RoomVent.create("r1", "cover.tilt", control_method="set_tilt_position"),
            RoomVent.create("r1", "cover.plain", control_method="open_close"),
        ]
        assert vc._count_open_vents(vents) == 1

    @pytest.mark.asyncio
    async def test_open_room_vents_drives_tilt_closed_vent_reporting_state_open(self):
        """The 'already open — skip' must not skip a tilt vent whose state
        lies: cycle start must command tilt 100 (#425 termination-reopen bug)."""
        ha = _make_ha_with_states(
            {"cover.v": {"state": "open", "attributes": {"current_tilt_position": 0}}}
        )
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="set_tilt_position")
        await vc.open_room_vents([vent])
        ha.set_cover_tilt_position.assert_awaited_once_with("cover.v", 100)

    @pytest.mark.asyncio
    async def test_open_room_vents_drives_partial_position_to_full(self):
        ha = _make_ha_with_states(
            {"cover.v": {"state": "open", "attributes": {"current_position": 50}}}
        )
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="set_position")
        await vc.open_room_vents([vent])
        ha.set_cover_position.assert_awaited_once_with("cover.v", 100)

    @pytest.mark.asyncio
    async def test_open_room_vents_still_skips_fully_open_vents(self):
        ha = _make_ha_with_states(
            {"cover.v": {"state": "open", "attributes": {"current_tilt_position": 100}}}
        )
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="set_tilt_position")
        await vc.open_room_vents([vent])
        ha.set_cover_tilt_position.assert_not_awaited()

    def test_get_vent_states_reports_airflow_for_tilt_vents(self):
        ha = _make_ha_with_states(
            {"cover.v": {"state": "open", "attributes": {"current_tilt_position": 0}}}
        )
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="set_tilt_position")
        assert vc.get_vent_states([vent]) == {"cover.v": "closed"}


# ---------------------------------------------------------------------------
# Issue #424: direct closes are guarded — a `toggle` vent already closed must
# never receive a toggle (it would INVERT open), and close errors never raise.
# ---------------------------------------------------------------------------


class TestForceCloseVents:
    @pytest.mark.asyncio
    async def test_closed_toggle_vent_is_not_toggled(self):
        ha = _make_ha_with_states({"cover.v": {"state": "closed", "attributes": {}}})
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="toggle")
        await vc.force_close_vents([vent])
        ha.toggle_cover.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_open_toggle_vent_is_toggled_closed(self):
        ha = _make_ha_with_states({"cover.v": {"state": "open", "attributes": {}}})
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="toggle")
        await vc.force_close_vents([vent])
        ha.toggle_cover.assert_awaited_once_with("cover.v")

    @pytest.mark.asyncio
    async def test_already_closed_open_close_vent_skipped(self):
        ha = _make_ha_with_states({"cover.v": {"state": "closed", "attributes": {}}})
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="open_close")
        await vc.force_close_vents([vent])
        ha.close_cover.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_close_errors_are_contained_not_raised(self):
        """The swallow must sit around a close that was genuinely attempted —
        a vent silently skipped would also "not raise", so assert the HA call
        happened and that the failure was logged rather than lost."""
        ha = _make_ha_with_states({"cover.v": {"state": "open", "attributes": {}}})
        ha.close_cover = AsyncMock(side_effect=RuntimeError("HA service error"))
        logger = AsyncMock()
        vc = VentController(ha, event_logger=logger)
        vent = RoomVent.create("r1", "cover.v", control_method="open_close")

        await vc.force_close_vents([vent])  # must not raise

        ha.close_cover.assert_awaited_once_with("cover.v")
        logger.log.assert_awaited_once()
        level, category, message, details = logger.log.await_args[0]
        assert level == "error"
        assert "HA service error" in message or "HA service error" in str(details)

    @pytest.mark.asyncio
    async def test_emergency_close_all_does_not_invert_closed_toggle_vents(self):
        """The one path whose purpose is 'make everything closed' must never
        OPEN a vent — the historical failure mode of unguarded toggles."""
        ha = _make_ha_with_states(
            {
                "cover.toggle_closed": {"state": "closed", "attributes": {}},
                "cover.toggle_open": {"state": "open", "attributes": {}},
            }
        )
        vc = VentController(ha)
        vents = [
            RoomVent.create("r1", "cover.toggle_closed", control_method="toggle"),
            RoomVent.create("r1", "cover.toggle_open", control_method="toggle"),
        ]
        await vc.close_all_zone_vents(vents)
        ha.toggle_cover.assert_awaited_once_with("cover.toggle_open")


class TestMethodAwareCloseSkip:
    """The close path's per-vent skip must be method-aware too (#425): a tilt
    vent opened via tilt=100 can report state='closed' (HA derives state from
    position, not tilt) — the old state-only skip never commanded tilt→0,
    while the floor math counted the vent open and the reconciler looped on
    're-closed' without ever closing it."""

    @pytest.mark.asyncio
    async def test_close_room_vents_closes_tilt_open_vent_reporting_state_closed(self):
        ha = _make_ha_with_states(
            {"cover.v": {"state": "closed", "attributes": {"current_tilt_position": 100}}}
        )
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="set_tilt_position")
        tc = _make_tc(min_open_vents=0)  # bypass damper — no floor in play
        closed = await vc.close_room_vents([vent], [vent], tc, {})
        assert closed is True
        ha.set_cover_tilt_position.assert_awaited_once_with("cover.v", 0)

    @pytest.mark.asyncio
    async def test_force_close_best_effort_for_unavailable_idempotent_vent(self):
        """An open_close vent whose entity flaked to 'unavailable' while
        physically open still gets a best-effort close — the command is
        idempotent and may reach the device."""
        ha = _make_ha_with_states({"cover.v": {"state": "unavailable", "attributes": {}}})
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="open_close")
        await vc.force_close_vents([vent])
        ha.close_cover.assert_awaited_once_with("cover.v")

    @pytest.mark.asyncio
    async def test_force_close_never_toggles_on_unavailable_state(self):
        """A toggle on an unavailable state is a coin flip — must be skipped."""
        ha = _make_ha_with_states({"cover.v": {"state": "unavailable", "attributes": {}}})
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="toggle")
        await vc.force_close_vents([vent])
        ha.toggle_cover.assert_not_awaited()


# ---------------------------------------------------------------------------
# Coverage additions: non-numeric position attributes and reopen event logging
# ---------------------------------------------------------------------------


class TestNonNumericPositionAttributes:
    """A position/tilt vent whose position attribute is garbage must fall back
    to the plain state string, not raise or misreport."""

    def test_is_open_falls_back_on_unparseable_position(self):
        ha = _make_ha_with_states(
            {"cover.v": {"state": "open", "attributes": {"current_position": "garbage"}}}
        )
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="set_position")
        assert vc._is_open(vent) is True  # state fallback, not float("garbage")

    def test_is_fully_open_missing_entity_is_false(self):
        ha = _make_ha_with_states({})
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="set_position")
        assert vc._is_fully_open(vent) is False

    def test_is_fully_open_falls_back_on_unparseable_position(self):
        ha = _make_ha_with_states(
            {"cover.v": {"state": "closed", "attributes": {"current_tilt_position": "n/a"}}}
        )
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="set_tilt_position")
        assert vc._is_fully_open(vent) is False  # state fallback says closed

    def test_is_fully_open_partial_position_is_not_fully_open(self):
        """state='open' at 50% must NOT count as fully open (#425)."""
        ha = _make_ha_with_states(
            {"cover.v": {"state": "open", "attributes": {"current_position": 50}}}
        )
        vc = VentController(ha)
        vent = RoomVent.create("r1", "cover.v", control_method="set_position")
        assert vc._is_fully_open(vent) is False


class TestMaxClosedReopenEventLog:
    @pytest.mark.asyncio
    async def test_force_reopen_writes_warning_event(self):
        """The safety reopen must be visible in the event log, with the room
        and vent ids in the message and details."""
        ha = _make_ha({"cover.v1": "closed"})
        logger = _RecordingLogger()
        ctrl = VentController(ha, event_logger=logger)
        tc = _make_tc(max_vent_closed_min=30)

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db.init_db(conn)
        room = Room(id="r1", name="R1", thermostat_entity_id=THERMO_ID)
        await db.upsert_room(conn, room)
        cycle = CycleLog.create(
            thermostat_entity_id=THERMO_ID, mode="cooling", rooms_json=json.dumps({"r1": {}})
        )
        cycle.id = "c1"
        await db.insert_cycle_log(conn, cycle)
        closed_at = datetime(2026, 4, 13, 10, 0)
        rcs = RoomCycleState(
            cycle_id="c1", room_id="r1", target_temp=74.0, vent_closed_at=closed_at
        )
        await db.upsert_room_cycle_state(conn, rcs)

        try:
            result = await ctrl.check_max_closed_duration(
                conn,
                {"r1": [_make_vent("r1", "cover.v1")]},
                {"r1": rcs},
                tc,
                now=closed_at + timedelta(minutes=45),
            )

            assert result == ["r1"]
            warnings = [e for e in logger.events if e[0] == "warning"]
            assert len(warnings) == 1
            _, category, message, details = warnings[0]
            assert category == "engine"
            assert "r1" in message and "cover.v1" in message
            assert details["room_id"] == "r1"
            assert details["max_vent_closed_min"] == 30
        finally:
            await conn.close()
