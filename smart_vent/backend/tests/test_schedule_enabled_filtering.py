"""Unit tests: the engine ignores disabled schedule blocks (Issue #359)."""

from __future__ import annotations

from datetime import UTC, datetime, time

from backend.engine import room_manager
from backend.models import Schedule


def _block(enabled: bool, *, days, start, end) -> Schedule:
    return Schedule.create(
        room_id="r",
        days_of_week=days,
        start_time=start,
        end_time=end,
        target_temp=70,
        enabled=enabled,
    )


# A Wednesday at 12:00 UTC (suite pins TZ=UTC, so local == UTC here).
NOW = datetime(2026, 1, 14, 12, 0, tzinfo=UTC)  # weekday() == 2 (Wed)


def test_disabled_block_not_matched_even_when_active() -> None:
    blk = _block(False, days=[2], start=time(8, 0), end=time(18, 0))
    assert room_manager._find_matching_schedule([blk], NOW) is None


def test_enabled_block_still_matches() -> None:
    blk = _block(True, days=[2], start=time(8, 0), end=time(18, 0))
    assert room_manager._find_matching_schedule([blk], NOW) is blk


def test_next_schedule_start_ignores_disabled() -> None:
    # A disabled block later today is not reported as the next upcoming start.
    blk = _block(False, days=[2], start=time(20, 0), end=time(22, 0))
    assert room_manager._next_schedule_start([blk], NOW) is None


def test_schedule_active_at_reflects_window() -> None:
    blk = _block(True, days=[2], start=time(8, 0), end=time(18, 0))
    assert room_manager.schedule_active_at(blk, NOW) is True
    # Outside the window (02:00) it is not active.
    early = datetime(2026, 1, 14, 2, 0, tzinfo=UTC)
    assert room_manager.schedule_active_at(blk, early) is False


def test_schedule_active_at_ignores_enabled_flag() -> None:
    # schedule_active_at answers "is a block running", regardless of enabled —
    # the expiry sweep relies on this to defer disabling.
    blk = _block(False, days=[2], start=time(8, 0), end=time(18, 0))
    assert room_manager.schedule_active_at(blk, NOW) is True
