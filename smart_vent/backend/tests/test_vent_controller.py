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


def _make_tc(**overrides) -> ThermostatConfig:
    defaults = {
        "thermostat_entity_id": THERMO_ID,
        "min_open_vents": 1,
        "max_vent_closed_min": 0,
    }
    defaults.update(overrides)
    return ThermostatConfig(**defaults)


def _make_vent(room_id: str, entity_id: str, control_method: str = "open_close") -> RoomVent:
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
