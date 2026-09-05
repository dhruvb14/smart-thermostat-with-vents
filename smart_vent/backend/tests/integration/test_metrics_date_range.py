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
        """A junk `limit` falls back to the documented default of 50 — not to
        the clamp minimum, which would silently truncate the page to one row."""
        conn = await _conn(client)
        for i in range(3):
            await _seed(conn, cycle_id=f"c{i}", started_at=_noon(i))

        resp = await client.get("/api/logs?limit=not-a-number")
        assert resp.status == 200
        assert [c["id"] for c in await resp.json()] == ["c0", "c1", "c2"]

        # A junk `offset` likewise falls back to 0 rather than skipping rows.
        resp = await client.get("/api/logs?limit=2&offset=nope")
        assert [c["id"] for c in await resp.json()] == ["c0", "c1"]

        # And an out-of-range `limit` is clamped, not rejected.
        resp = await client.get("/api/logs?limit=0")
        assert [c["id"] for c in await resp.json()] == ["c0"]


# ---------------------------------------------------------------------------
# Malformed `start` / `end` bounds (Issue #606)
# ---------------------------------------------------------------------------

# Every route that calls ``_parse_date_range``. The point of parametrising over
# the whole family is that #606 had *two* symptoms depending on which consumer
# received the unvalidated string — the two ``/summary`` routes fed it to
# ``datetime.fromisoformat`` and returned 500, the other nine dropped it into a
# SQL ``BETWEEN`` and compared it lexicographically. A fix that only patched the
# two crashing routes must fail this list.
_DATE_RANGE_CONSUMERS = [
    f"/api/metrics/thermostats/{THERMO}/summary",
    "/api/metrics/thermostats/summary",
    f"/api/metrics/thermostats/{THERMO}/timeseries",
    f"/api/metrics/thermostats/{THERMO}/rooms",
    f"/api/metrics/thermostats/{THERMO}/cycles-vs-outside-temp",
    f"/api/metrics/thermostats/{THERMO}/eco-impact",
    "/api/metrics/thermostats/eco-impact",
    f"/api/metrics/thermostats/{THERMO}/overshoot-histogram",
    f"/api/metrics/thermostats/{THERMO}/hour-heatmap",
    f"/api/metrics/thermostats/{THERMO}/vent-timeline",
    "/api/metrics/export.csv",
]

# The shapes from the issue's reproduction table. The last one is the
# inconsistency #606 also closed: ``datetime.fromisoformat`` accepted it (and
# silently produced a window whose end was a *datetime* string) where
# ``date.fromisoformat`` — now the pipeline's only parser — rejects it.
_MALFORMED_DATES = [
    "totally-not-a-date",
    "2025-6-1",
    "2025-06-31",
    "June 2025",
    "2025-06-01T00:00:00",
    "12/31/2026",
]


class TestMalformedDateBounds:
    """`_parse_date_range` is the single place that decides what a date is, and
    it must never hand a consumer a string that consumer cannot parse (#606)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", _DATE_RANGE_CONSUMERS)
    async def test_malformed_end_rejected_uniformly_across_the_family(self, client, path):
        resp = await client.get(f"{path}?end=totally-not-a-date")
        assert resp.status == 400, f"{path} -> {resp.status}"
        assert await resp.json() == {"error": "end must be an ISO date (YYYY-MM-DD)"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", _DATE_RANGE_CONSUMERS)
    async def test_malformed_start_rejected_uniformly_across_the_family(self, client, path):
        """`start` was never parsed at all when supplied — the old `try` block
        only ran on the branch where `start` was *absent*."""
        resp = await client.get(f"{path}?start=totally-not-a-date&end={_local_date(0)}")
        assert resp.status == 400, f"{path} -> {resp.status}"
        assert await resp.json() == {"error": "start must be an ISO date (YYYY-MM-DD)"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", _MALFORMED_DATES)
    async def test_every_malformed_shape_rejected_on_both_summary_routes(self, client, bad):
        """The two routes that used to return 500, over each shape from #606."""
        for path in (
            f"/api/metrics/thermostats/{THERMO}/summary",
            "/api/metrics/thermostats/summary",
        ):
            resp = await client.get(f"{path}?end={bad}")
            assert resp.status == 400, f"{path}?end={bad} -> {resp.status}"
            assert await resp.json() == {"error": "end must be an ISO date (YYYY-MM-DD)"}

            resp = await client.get(f"{path}?start={bad}&end={_local_date(0)}")
            assert resp.status == 400, f"{path}?start={bad} -> {resp.status}"
            assert await resp.json() == {"error": "start must be an ISO date (YYYY-MM-DD)"}

    @pytest.mark.asyncio
    async def test_end_is_validated_before_start(self, client):
        """Both bounds malformed reports `end`, the bound parsed first. Pinned so
        the message is deterministic for a caller that fixes one at a time."""
        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO}/summary?start=nope&end=also-nope"
        )
        assert resp.status == 400
        assert await resp.json() == {"error": "end must be an ISO date (YYYY-MM-DD)"}

    @pytest.mark.asyncio
    async def test_rejection_body_is_json_and_carries_no_exception_text(self, client):
        """CWE-209 / security alert #4: the 400 body is a fixed, user-safe
        message. Python's own text ("Invalid isoformat string: ...") echoes the
        caller's input back and must not appear."""
        resp = await client.get(f"/api/metrics/thermostats/{THERMO}/summary?end=totally-not-a-date")
        assert resp.status == 400
        assert resp.content_type == "application/json"
        body = await resp.text()
        assert body == '{"error": "end must be an ISO date (YYYY-MM-DD)"}'
        assert "isoformat" not in body
        assert "ValueError" not in body
        assert "totally-not-a-date" not in body
        assert "Traceback" not in body

    @pytest.mark.asyncio
    async def test_lexicographic_window_bug_is_gone(self, client):
        """#606's silent half. With cycles seeded inside the intended window, the
        old parser answered ``end=12/31/2026`` with an empty result (because
        '1' < '2') and ``end=totally-not-a-date`` with every row (because
        't' > '2') — both 200, both wrong, in opposite directions. Now both are
        rejected identically, and the well-formed window still sees all three."""
        conn = await _conn(client)
        await _seed(conn, cycle_id="c0", started_at=_noon(0))
        await _seed(conn, cycle_id="c2", started_at=_noon(2))
        await _seed(conn, cycle_id="c5", started_at=_noon(5))

        statuses = {}
        for bad in ("12/31/2026", "totally-not-a-date"):
            resp = await client.get(f"/api/metrics/thermostats/{THERMO}/summary?days=7&end={bad}")
            statuses[bad] = resp.status
        assert statuses == {"12/31/2026": 400, "totally-not-a-date": 400}

        # The intended window is unchanged: 3 cycles × 20 minutes = 3600s.
        resp = await client.get(f"/api/metrics/thermostats/{THERMO}/summary?days=7")
        assert resp.status == 200
        data = await resp.json()
        assert data["cycle_count"] == 3
        assert data["heating_seconds"] + data["cooling_seconds"] == 3600

    @pytest.mark.asyncio
    async def test_bounds_are_normalised_so_consumers_compare_like_with_like(self, client):
        """``date.fromisoformat`` accepts ISO basic format; the parser re-emits
        every bound through ``date.isoformat()``, so the SQL ``BETWEEN`` and the
        ``start < floor`` retention clamp are both string compares that are
        correct *by construction*. Before #606 a `20250601`-style bound went
        through raw and compared lexicographically against `2025-06-01`."""
        conn = await _conn(client)
        await _seed(conn, cycle_id="today", started_at=_noon(0))
        await _seed(conn, cycle_id="old", started_at=_noon(20))

        compact = _local_date(0).replace("-", "")
        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO}/summary?start={compact}&end={compact}"
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["start_date"] == _local_date(0)
        assert data["end_date"] == _local_date(0)
        assert data["cycle_count"] == 1

    @pytest.mark.asyncio
    async def test_days_shorthand_keeps_its_lenient_fallback(self, client):
        """Deliberately *not* tightened by #606: `days` is a convenience whose
        default is harmless, whereas `start`/`end` define the window whose
        contents the caller is about to reason about."""
        resp = await client.get(f"/api/metrics/thermostats/{THERMO}/summary?days=lots")
        assert resp.status == 200
        data = await resp.json()
        assert data["start_date"] == _local_date(6)
        assert data["end_date"] == _local_date(0)

        # Junk `days` combined with a *valid* end still falls back, not rejects.
        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO}/summary?days=lots&end={_local_date(1)}"
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["start_date"] == _local_date(7)
        assert data["end_date"] == _local_date(1)

    @pytest.mark.asyncio
    async def test_empty_bounds_are_treated_as_absent_not_malformed(self, client):
        """``<input type="date">`` submits an empty string when cleared, and the
        web UI is the one caller that cannot produce a malformed date. `?start=&end=`
        must keep meaning "use the defaults", not 400."""
        resp = await client.get(f"/api/metrics/thermostats/{THERMO}/summary?start=&end=")
        assert resp.status == 200
        data = await resp.json()
        assert data["start_date"] == _local_date(6)
        assert data["end_date"] == _local_date(0)

    @pytest.mark.asyncio
    async def test_malformed_bound_is_rejected_before_the_route_own_validation(self, client):
        """The CSV export validates `scope` itself. The date bounds are parsed
        first, so a request that is wrong in both ways reports the date — the
        parser is the single gate, not a second opinion layered after."""
        resp = await client.get("/api/metrics/export.csv?scope=bogus&end=totally-not-a-date")
        assert resp.status == 400
        assert await resp.json() == {"error": "end must be an ISO date (YYYY-MM-DD)"}

        # scope validation still reachable with well-formed dates.
        resp = await client.get(f"/api/metrics/export.csv?scope=bogus&end={_local_date(0)}")
        assert resp.status == 400
        assert await resp.json() == {"error": "scope must be 'home' or 'thermostat'"}
