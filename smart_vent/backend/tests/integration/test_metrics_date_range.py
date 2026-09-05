"""Date-range, paging, and retention-clamp behaviour for the metrics and
cycle-log APIs (Issue #403).

The metrics endpoints already accepted ``start``/``end``; this suite covers the
new ``days=N`` shorthand, the retention-window clamp on both the metrics and
cycle-log surfaces, and ``limit``/``offset``/``start``/``end`` paging on
``/api/logs``.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta

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
        """#606's silent half, driven through a consumer that actually had it.

        Care with which route proves what: the two ``/summary`` routes
        *crashed* on a malformed bound (500). The silent wrong window belonged
        to the other nine, which drop the raw string into a SQL ``BETWEEN`` and
        compare it lexicographically against real ISO dates. So the seeded
        assertions below go through ``/export.csv`` — measured on the pre-fix
        code, ``end=12/31/2026`` returned 200 with ZERO rows (because
        ``'1' < '2'``) while ``end=totally-not-a-date`` returned 200 with EVERY
        row (because ``'t' > '2'``): same class of input, opposite wrong
        answers, no error either way. Both are now rejected identically.

        ``/summary`` is driven alongside so one test covers both halves of
        #606, and each route's well-formed window is asserted afterwards so the
        rejection is demonstrably about the bound and not about the route."""
        conn = await _conn(client)
        await _seed(conn, cycle_id="c0", started_at=_noon(0))
        await _seed(conn, cycle_id="c2", started_at=_noon(2))
        await _seed(conn, cycle_id="c5", started_at=_noon(5))

        statuses = {}
        for bad in ("12/31/2026", "totally-not-a-date"):
            for path in (
                f"/api/metrics/thermostats/{THERMO}/summary",  # used to 500
                "/api/metrics/export.csv",  # used to 200 with the wrong window
            ):
                resp = await client.get(f"{path}?days=7&end={bad}")
                statuses[(path, bad)] = resp.status
        assert set(statuses.values()) == {400}, statuses

        # The lexicographic consumer's intended window is unchanged: all three.
        resp = await client.get("/api/metrics/export.csv?days=7")
        assert resp.status == 200
        rows = list(csv.reader(io.StringIO(await resp.text())))
        assert {r[0] for r in rows[1:]} == {"c0", "c2", "c5"}

        # …and so is the crashing consumer's: 3 cycles × 20 minutes = 3600s.
        resp = await client.get(f"/api/metrics/thermostats/{THERMO}/summary?days=7")
        assert resp.status == 200
        data = await resp.json()
        assert data["cycle_count"] == 3
        assert data["heating_seconds"] + data["cooling_seconds"] == 3600

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query",
        [
            "end=0001-01-01",  # `end - default_days` underflows `date.min`
            "days=99999999",  # so does an absurd look-back from today
            "days=999999999999999999999",  # and this one cannot even reach C int
        ],
    )
    async def test_out_of_range_windows_are_clamped_not_a_500(self, client, query):
        """The subtraction that builds `start` is the one arithmetic step in the
        parser that can leave the date domain, and it raises ``OverflowError``,
        not ``ValueError`` — so no ``except ValueError`` catches it and
        ``security_headers_middleware``'s catch-all turned it into a bare 500.

        Reachable from the UI, not just from a script: clear the Metrics page's
        start-date input and type year 0001 into the end-date input, and
        ``<input type="date">`` emits the perfectly well-formed ``0001-01-01``.
        ``days`` is MCP-exposed via ``_DATE_RANGE_QUERY_PARAMS``.

        #606's premise is that no query string crashes a read-only metrics
        query, so these clamp to the widest expressible window instead."""
        resp = await client.get(f"/api/metrics/thermostats/{THERMO}/summary?{query}")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        # Whatever the clamp produced, it is a real ISO date the consumers can
        # parse — which is the property the whole parser exists to guarantee.
        assert date.fromisoformat(data["start_date"])
        assert date.fromisoformat(data["end_date"])

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


# ---------------------------------------------------------------------------
# `/api/logs` bounds — the twelfth route in #606's reproduction table
# ---------------------------------------------------------------------------


class TestLogsBoundValidation:
    """``/api/logs`` had the same silent-wrong-window defect as the metrics
    family and is fixed the same way, but it is NOT a ``_parse_date_range``
    consumer (so it is absent from ``_DATE_RANGE_CONSUMERS`` above).

    It has its own bounds — ``since``/``until``, aliased ``start``/``end`` —
    added by #403 to "mirror the metrics API", and they go straight into
    ``get_cycle_logs``' ``started_at >= ?`` / ``<= ?``, which is a
    *lexicographic* string compare against stored ISO timestamps. Measured on
    the pre-fix code with two completed cycles seeded: ``?end=12/31/2026`` →
    200 with zero cycles, ``?end=totally-not-a-date`` → 200 with every cycle.
    That is MCP-exposed — ``build_tool_specs`` emits a ``get_logs`` tool whose
    bounds are free-form strings — so an assistant asked for June's cycles got
    an empty list and would report the system never ran.

    The parser differs from the metrics one deliberately: this surface
    advertises "ISO date/datetime" and the Logs page sends a full
    ``toISOString()`` instant, so it validates through ``_iso_instant_param``.
    Same 400 contract, wider accepted set.
    """

    @pytest.fixture
    async def seeded(self, client):
        conn = await _conn(client)
        await _seed(conn, cycle_id="c0", started_at=_noon(0))
        await _seed(conn, cycle_id="c1", started_at=_noon(1))
        return client

    @pytest.mark.asyncio
    @pytest.mark.parametrize("param", ["start", "end", "since", "until"])
    @pytest.mark.parametrize("bad", ["12/31/2026", "totally-not-a-date", "June 2025", "2025-6-1"])
    async def test_every_bound_and_alias_rejects_a_malformed_value(self, seeded, param, bad):
        resp = await seeded.get(f"/api/logs?{param}={bad}")
        assert resp.status == 400, f"{param}={bad} -> {resp.status}"
        # The 400 names the alias the caller actually used, so "fix this knob"
        # is unambiguous, and carries no exception text (CWE-209).
        body = await resp.text()
        assert body == f'{{"error": "{param} must be an ISO date or datetime"}}'
        assert "isoformat" not in body
        assert bad not in body

    @pytest.mark.asyncio
    async def test_both_documented_shapes_still_pass(self, seeded):
        """A bare ISO date and a full ISO instant — what the OpenAPI param
        description promises, and what ``Logs.tsx`` actually sends
        (``new Date(...).toISOString()``, trailing ``Z`` and all)."""
        for good in (_local_date(5), "2020-01-01T00:00:00.000Z", "2020-01-01T00:00:00"):
            resp = await seeded.get(f"/api/logs?since={good}")
            assert resp.status == 200, f"{good} -> {await resp.text()}"
            assert {c["id"] for c in await resp.json()} == {"c0", "c1"}

    @pytest.mark.asyncio
    async def test_empty_and_absent_bounds_are_unchanged(self, seeded):
        """Absent means "no filter"; an empty string (what a cleared input
        submits) means the same, not "malformed"."""
        for query in ("", "?since=&until=", "?start=&end="):
            resp = await seeded.get(f"/api/logs{query}")
            assert resp.status == 200
            assert {c["id"] for c in await resp.json()} == {"c0", "c1"}

    @pytest.mark.asyncio
    async def test_primary_bound_wins_over_its_alias(self, seeded):
        """``since``/``until`` take precedence over ``start``/``end`` (#403), and
        validation follows the value that is actually used — so a junk alias
        alongside a good primary is ignored rather than reported."""
        resp = await seeded.get(f"/api/logs?since={_local_date(5)}&start=totally-not-a-date")
        assert resp.status == 200
        assert {c["id"] for c in await resp.json()} == {"c0", "c1"}

        resp = await seeded.get(f"/api/logs?until={_local_date(0)}&end=totally-not-a-date")
        assert resp.status == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize("param", ["since", "until"])
    async def test_event_feed_bounds_are_validated_the_same_way(self, seeded, param):
        """``/api/logs/events`` is the same query shape one route over
        (``timestamp >= ?`` / ``<= ?``), so it goes through the same validator.
        Leaving it out would have reproduced the exact asymmetry #606 is about,
        one route further along."""
        resp = await seeded.get(f"/api/logs/events?{param}=12/31/2026")
        assert resp.status == 400
        assert await resp.json() == {"error": f"{param} must be an ISO date or datetime"}

        resp = await seeded.get(f"/api/logs/events?{param}={_local_date(0)}")
        assert resp.status == 200
