"""Date-range, paging, and retention-clamp behaviour for the metrics and
cycle-log APIs (Issue #403).

The metrics endpoints already accepted ``start``/``end``; this suite covers the
new ``days=N`` shorthand, the retention-window clamp on both the metrics and
cycle-log surfaces, and ``limit``/``offset``/``start``/``end`` paging on
``/api/logs``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend import db
from backend.models import CycleLog

THERMO = "climate.thermo_a"


async def _conn(client):
    return await client.app["scheduler"].get_db()


async def _seed(
    conn,
    *,
    cycle_id: str,
    started_at: datetime,
    duration: timedelta = timedelta(minutes=20),
    mode: str = "cooling",
) -> None:
    log_ = CycleLog(
        id=cycle_id,
        thermostat_entity_id=THERMO,
        started_at=started_at,
        mode=mode,
        rooms_json="{}",
    )
    await db.insert_cycle_log(conn, log_)
    await db.close_cycle_log(
        conn, cycle_id, ended_at=started_at + duration, ended_reason="completed"
    )


def _noon(days_ago: int) -> datetime:
    """`days_ago` days before today at 12:00 UTC — far from local midnight so
    the local-date bucketing never flips the calendar day."""
    return datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(
        days=days_ago
    )


def _local_date(days_ago: int) -> str:
    return (datetime.now().date() - timedelta(days=days_ago)).isoformat()  # noqa: DTZ005


# ---------------------------------------------------------------------------
# Metrics: explicit range, default window, days shorthand, retention clamp
# ---------------------------------------------------------------------------


class TestMetricsDateRange:
    @pytest.mark.asyncio
    async def test_explicit_range_returns_only_that_range(self, client):
        conn = await _conn(client)
        await _seed(conn, cycle_id="today", started_at=_noon(0))
        await _seed(conn, cycle_id="old", started_at=_noon(20))

        # A single-day window on today sees only today's cycle.
        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO}/summary?start={_local_date(0)}&end={_local_date(0)}"
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["cycle_count"] == 1
        assert data["start_date"] == _local_date(0)
        assert data["end_date"] == _local_date(0)

        # A single-day window 20 days back sees only the old cycle.
        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO}/summary"
            f"?start={_local_date(20)}&end={_local_date(20)}"
        )
        data = await resp.json()
        assert data["cycle_count"] == 1
        assert data["start_date"] == _local_date(20)

    @pytest.mark.asyncio
    async def test_default_window_is_last_seven_days(self, client):
        conn = await _conn(client)
        await _seed(conn, cycle_id="today", started_at=_noon(0))
        await _seed(conn, cycle_id="old", started_at=_noon(20))

        resp = await client.get(f"/api/metrics/thermostats/{THERMO}/summary")
        assert resp.status == 200
        data = await resp.json()
        # Default retention is 30 days, so the 20-day-old cycle is retained but
        # falls outside the default 7-day window.
        assert data["start_date"] == _local_date(6)
        assert data["end_date"] == _local_date(0)
        assert data["cycle_count"] == 1

    @pytest.mark.asyncio
    async def test_days_shorthand_sets_window(self, client):
        conn = await _conn(client)
        await _seed(conn, cycle_id="today", started_at=_noon(0))
        await _seed(conn, cycle_id="d2", started_at=_noon(2))
        await _seed(conn, cycle_id="d5", started_at=_noon(5))

        resp = await client.get(f"/api/metrics/thermostats/{THERMO}/summary?days=3")
        assert resp.status == 200
        data = await resp.json()
        # Last 3 days inclusive of today: today, -1, -2. The -5 cycle is out.
        assert data["start_date"] == _local_date(2)
        assert data["end_date"] == _local_date(0)
        assert data["cycle_count"] == 2

    @pytest.mark.asyncio
    async def test_start_clamped_to_retention_floor(self, client):
        conn = await _conn(client)
        await db.set_system_setting(conn, "cycle_log_retention_days", "5")

        # Ask for a 30-day window; the effective start is clamped to today-5.
        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO}/summary"
            f"?start={_local_date(30)}&end={_local_date(0)}"
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["start_date"] == _local_date(5)
        assert data["end_date"] == _local_date(0)


# ---------------------------------------------------------------------------
# Cycle logs: paging + date range + retention clamp
# ---------------------------------------------------------------------------


class TestLogsPagingAndRange:
    @pytest.mark.asyncio
    async def test_limit_and_offset_page_history(self, client):
        conn = await _conn(client)
        # Ordered started_at DESC → newest first.
        await _seed(conn, cycle_id="c0", started_at=_noon(0))
        await _seed(conn, cycle_id="c1", started_at=_noon(1))
        await _seed(conn, cycle_id="c2", started_at=_noon(2))

        resp = await client.get("/api/logs?limit=1&offset=0")
        assert resp.status == 200
        page0 = await resp.json()
        assert [c["id"] for c in page0] == ["c0"]

        resp = await client.get("/api/logs?limit=1&offset=1")
        page1 = await resp.json()
        assert [c["id"] for c in page1] == ["c1"]

    @pytest.mark.asyncio
    async def test_start_end_filter_by_date(self, client):
        conn = await _conn(client)
        await _seed(conn, cycle_id="recent", started_at=_noon(1))
        await _seed(conn, cycle_id="older", started_at=_noon(10))

        # start on day -5 (ISO date) excludes the 10-day-old cycle.
        resp = await client.get(f"/api/logs?start={_local_date(5)}")
        assert resp.status == 200
        ids = {c["id"] for c in await resp.json()}
        assert ids == {"recent"}

    @pytest.mark.asyncio
    async def test_start_clamped_to_retention_excludes_purged_window(self, client):
        conn = await _conn(client)
        await db.set_system_setting(conn, "cycle_log_retention_days", "5")
        await _seed(conn, cycle_id="recent", started_at=_noon(2))
        await _seed(conn, cycle_id="ancient", started_at=_noon(30))

        # Without clamping, start 40 days back would surface the 30-day-old
        # cycle. Clamped to the retention floor (today-5) it must not.
        resp = await client.get(f"/api/logs?start={_local_date(40)}")
        assert resp.status == 200
        ids = {c["id"] for c in await resp.json()}
        assert ids == {"recent"}

    @pytest.mark.asyncio
    async def test_bad_limit_falls_back_to_default(self, client):
        conn = await _conn(client)
        await _seed(conn, cycle_id="c0", started_at=_noon(0))

        resp = await client.get("/api/logs?limit=not-a-number")
        assert resp.status == 200
        assert len(await resp.json()) == 1
