"""Tests for the centralized timezone helpers (backend/tz.py).

conftest.py pins TZ=UTC for the whole suite; these tests temporarily override
TZ via monkeypatch and restore the UTC baseline (env + tzset) afterwards so
they cannot leak a non-UTC local zone into other test modules.
"""

from __future__ import annotations

import os
import time as _time
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backend import tz


@pytest.fixture(autouse=True)
def _restore_utc_baseline():
    """Restore the suite-wide TZ=UTC baseline (and tzset) after each test."""
    yield
    os.environ["TZ"] = "UTC"
    if hasattr(_time, "tzset"):
        _time.tzset()


class TestGetTimezone:
    def test_reads_tz_env(self, monkeypatch):
        monkeypatch.setenv("TZ", "America/New_York")
        assert tz.get_timezone() == ZoneInfo("America/New_York")

    def test_invalid_tz_falls_back_without_raising(self, monkeypatch):
        monkeypatch.setenv("TZ", "Not/ARealZone")
        result = tz.get_timezone()  # must not raise
        assert result is not None

    def test_unset_tz_falls_back_to_system_local(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)
        if hasattr(_time, "tzset"):
            _time.tzset()
        result = tz.get_timezone()
        assert result is not None


class TestNowAndToday:
    def test_now_local_is_aware_in_configured_zone(self, monkeypatch):
        monkeypatch.setenv("TZ", "America/New_York")
        now = tz.now_local()
        assert now.tzinfo is not None
        assert now.tzinfo == ZoneInfo("America/New_York")

    def test_today_local_matches_now_local_date(self, monkeypatch):
        monkeypatch.setenv("TZ", "America/New_York")
        assert tz.today_local() == tz.now_local().date()
        assert isinstance(tz.today_local(), date)


class TestToLocal:
    def test_aware_utc_converted_to_local_instant(self, monkeypatch):
        monkeypatch.setenv("TZ", "America/New_York")
        # 03:00 UTC on a January day == 22:00 EST (UTC-5), same instant.
        utc_dt = datetime(2026, 1, 15, 3, 0, tzinfo=UTC)
        local = tz.to_local(utc_dt)
        assert local.hour == 22
        assert local.utcoffset() == timedelta(hours=-5)
        assert local == utc_dt  # same instant

    def test_naive_treated_as_local_wallclock(self, monkeypatch):
        monkeypatch.setenv("TZ", "America/New_York")
        naive = datetime(2026, 1, 15, 22, 0)
        local = tz.to_local(naive)
        assert local.tzinfo == ZoneInfo("America/New_York")
        assert local.hour == 22  # wall-clock preserved, not shifted


class TestToLocalNaive:
    def test_aware_utc_converted_and_stripped(self, monkeypatch):
        monkeypatch.setenv("TZ", "America/New_York")
        utc_dt = datetime(2026, 1, 15, 3, 0, tzinfo=UTC)
        result = tz.to_local_naive(utc_dt)
        assert result.tzinfo is None
        # 03:00 UTC -> 22:00 EST the *previous* day (UTC-5 crosses midnight).
        assert result == datetime(2026, 1, 14, 22, 0)

    def test_naive_returned_unchanged(self, monkeypatch):
        monkeypatch.setenv("TZ", "America/New_York")
        naive = datetime(2026, 1, 15, 22, 0)
        result = tz.to_local_naive(naive)
        assert result is naive


class TestNowUtc:
    """PLENUM_CLOCK_OVERRIDE pins now_utc() for E2E golden determinism (#456).

    monkeypatch.setenv/delenv is auto-reverted per test, so the override never
    leaks between tests.
    """

    def test_no_override_returns_current_utc(self, monkeypatch):
        monkeypatch.delenv("PLENUM_CLOCK_OVERRIDE", raising=False)
        before = datetime.now(UTC)
        result = tz.now_utc()
        after = datetime.now(UTC)
        assert result.tzinfo == UTC
        assert before <= result <= after

    def test_aware_override_converted_to_utc(self, monkeypatch):
        # Wed 10:00 ET (UTC-4) is the same instant as 14:00 UTC.
        monkeypatch.setenv("PLENUM_CLOCK_OVERRIDE", "2025-06-04T10:00:00-04:00")
        assert tz.now_utc() == datetime(2025, 6, 4, 14, 0, tzinfo=UTC)

    def test_naive_override_read_as_utc(self, monkeypatch):
        monkeypatch.setenv("PLENUM_CLOCK_OVERRIDE", "2025-06-04T14:00:00")
        result = tz.now_utc()
        assert result == datetime(2025, 6, 4, 14, 0, tzinfo=UTC)
        assert result.tzinfo == UTC

    def test_unparseable_override_falls_back_to_real_now(self, monkeypatch):
        monkeypatch.setenv("PLENUM_CLOCK_OVERRIDE", "not-a-timestamp")
        before = datetime.now(UTC)
        result = tz.now_utc()  # must not raise
        after = datetime.now(UTC)
        assert result.tzinfo == UTC
        assert before <= result <= after
