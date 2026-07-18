"""
Phase 1 of Issue #85 — schema, outside-temperature capture, rollup jobs.

Covers:
  - 1a: daily_thermostat_metrics + monthly_thermostat_metrics tables exist
        with the expected columns after init_db().
  - 1c: cycle_logs has the new outside_temp_at_start / outside_temp_at_end
        columns; close_cycle_log persists the end value.
  - 1d/1e: db.rollup_daily_metrics + db.rollup_monthly_metrics buckets
           cycles correctly, is idempotent, and skips in-flight rows.
  - Scheduler.run_daily_metrics_rollup / run_monthly_metrics_rollup
    drive the rollup over a sensible default window, and the cron jobs
    are registered.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import aiosqlite
import pytest

from backend import db
from backend.models import CycleLog
from backend.scheduler import Scheduler

THERMO_A = "climate.thermo_a"
THERMO_B = "climate.thermo_b"


async def _setup_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    return conn


async def _table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return {r["name"] for r in rows}


async def _insert_finished_cycle(
    conn: aiosqlite.Connection,
    *,
    cycle_id: str,
    thermostat: str,
    started_at: datetime,
    duration: timedelta,
    mode: str = "cooling",
    ended_reason: str = "completed",
    outside_temp_at_start: float | None = None,
    outside_temp_at_end: float | None = None,
) -> None:
    log_ = CycleLog(
        id=cycle_id,
        thermostat_entity_id=thermostat,
        started_at=started_at,
        mode=mode,
        rooms_json="{}",
        outside_temp_at_start=outside_temp_at_start,
    )
    await db.insert_cycle_log(conn, log_)
    await db.close_cycle_log(
        conn,
        cycle_id,
        ended_at=started_at + duration,
        ended_reason=ended_reason,
        outside_temp_at_end=outside_temp_at_end,
    )


# ---------------------------------------------------------------------------
# 1a: schema
# ---------------------------------------------------------------------------


class TestSchema:
    @pytest.mark.asyncio
    async def test_daily_metrics_table_columns(self):
        conn = await _setup_db()
        try:
            cols = await _table_columns(conn, "daily_thermostat_metrics")
            expected = {
                "date",
                "thermostat_entity_id",
                "heating_seconds",
                "cooling_seconds",
                "cycle_count",
                "completed_count",
                "timeout_count",
                "aborted_count",
                "avg_cycle_duration_seconds",
                "avg_outside_temp_at_start",
                "avg_outside_temp_at_end",
                "updated_at",
            }
            assert expected.issubset(cols)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_monthly_metrics_table_columns(self):
        conn = await _setup_db()
        try:
            cols = await _table_columns(conn, "monthly_thermostat_metrics")
            expected = {
                "month",
                "thermostat_entity_id",
                "heating_seconds",
                "cooling_seconds",
                "cycle_count",
                "completed_count",
                "timeout_count",
                "aborted_count",
                "avg_cycle_duration_seconds",
                "avg_outside_temp_at_start",
                "avg_outside_temp_at_end",
                "updated_at",
            }
            assert expected.issubset(cols)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_init_db_is_idempotent(self):
        # Running init twice must not crash on the new tables (the SCHEMA
        # uses CREATE TABLE IF NOT EXISTS and migrations swallow duplicates).
        conn = await _setup_db()
        try:
            await db.init_db(conn)  # second invocation
            cols = await _table_columns(conn, "daily_thermostat_metrics")
            assert "date" in cols
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# 1c: cycle_logs new columns + persistence
# ---------------------------------------------------------------------------


class TestCycleLogOutsideTempColumns:
    @pytest.mark.asyncio
    async def test_columns_added(self):
        conn = await _setup_db()
        try:
            cols = await _table_columns(conn, "cycle_logs")
            assert "outside_temp_at_start" in cols
            assert "outside_temp_at_end" in cols
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_insert_and_close_round_trip(self):
        conn = await _setup_db()
        try:
            log_ = CycleLog(
                id="cyc-1",
                thermostat_entity_id=THERMO_A,
                started_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
                mode="cooling",
                rooms_json="{}",
                outside_temp_at_start=85.5,
            )
            await db.insert_cycle_log(conn, log_)
            await db.close_cycle_log(
                conn,
                "cyc-1",
                ended_at=datetime(2026, 4, 20, 12, 30, tzinfo=UTC),
                ended_reason="completed",
                outside_temp_at_end=82.1,
            )
            got = await db.get_cycle_log(conn, "cyc-1")
            assert got is not None
            assert got.outside_temp_at_start == 85.5
            assert got.outside_temp_at_end == 82.1
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_columns_default_to_null(self):
        conn = await _setup_db()
        try:
            log_ = CycleLog(
                id="cyc-2",
                thermostat_entity_id=THERMO_A,
                started_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
                mode="heating",
                rooms_json="{}",
                # outside_temp_at_start intentionally omitted
            )
            await db.insert_cycle_log(conn, log_)
            got = await db.get_cycle_log(conn, "cyc-2")
            assert got.outside_temp_at_start is None
            assert got.outside_temp_at_end is None
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# 1d/1e: rollup logic
# ---------------------------------------------------------------------------


class TestDailyRollup:
    @pytest.mark.asyncio
    async def test_aggregates_cycles_per_day_per_thermostat(self):
        conn = await _setup_db()
        try:
            base = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
            # Two cooling cycles for thermo A on day 0
            await _insert_finished_cycle(
                conn,
                cycle_id="a1",
                thermostat=THERMO_A,
                started_at=base,
                duration=timedelta(minutes=30),
                outside_temp_at_start=80.0,
                outside_temp_at_end=78.0,
            )
            await _insert_finished_cycle(
                conn,
                cycle_id="a2",
                thermostat=THERMO_A,
                started_at=base + timedelta(hours=2),
                duration=timedelta(minutes=10),
                outside_temp_at_start=82.0,
                outside_temp_at_end=80.0,
            )
            # One heating cycle for thermo B on day 0
            await _insert_finished_cycle(
                conn,
                cycle_id="b1",
                thermostat=THERMO_B,
                started_at=base + timedelta(hours=4),
                duration=timedelta(minutes=20),
                mode="heating",
            )

            day = base.date().isoformat()
            n = await db.rollup_daily_metrics(conn, day, day)
            assert n == 2  # one row per (date, thermostat)

            rows = await db.get_daily_thermostat_metrics(conn, start_date=day, end_date=day)
            assert len(rows) == 2
            by_t = {r["thermostat_entity_id"]: r for r in rows}

            ra = by_t[THERMO_A]
            assert ra["cycle_count"] == 2
            assert ra["completed_count"] == 2
            assert ra["timeout_count"] == 0
            assert ra["aborted_count"] == 0
            assert ra["cooling_seconds"] == 30 * 60 + 10 * 60
            assert ra["heating_seconds"] == 0
            assert ra["avg_cycle_duration_seconds"] == pytest.approx((30 * 60 + 10 * 60) / 2)
            assert ra["avg_outside_temp_at_start"] == pytest.approx(81.0)
            assert ra["avg_outside_temp_at_end"] == pytest.approx(79.0)

            rb = by_t[THERMO_B]
            assert rb["cycle_count"] == 1
            assert rb["heating_seconds"] == 20 * 60
            assert rb["cooling_seconds"] == 0
            assert rb["avg_outside_temp_at_start"] is None
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_classifies_ended_reason_buckets(self):
        conn = await _setup_db()
        try:
            base = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
            await _insert_finished_cycle(
                conn,
                cycle_id="c1",
                thermostat=THERMO_A,
                started_at=base,
                duration=timedelta(minutes=10),
                ended_reason="completed",
            )
            await _insert_finished_cycle(
                conn,
                cycle_id="c2",
                thermostat=THERMO_A,
                started_at=base + timedelta(hours=1),
                duration=timedelta(minutes=10),
                ended_reason="aborted: timeout (3.0h)",
            )
            await _insert_finished_cycle(
                conn,
                cycle_id="c3",
                thermostat=THERMO_A,
                started_at=base + timedelta(hours=2),
                duration=timedelta(minutes=10),
                ended_reason="aborted: system disabled",
            )

            day = base.date().isoformat()
            await db.rollup_daily_metrics(conn, day, day)
            rows = await db.get_daily_thermostat_metrics(conn, start_date=day, end_date=day)
            assert len(rows) == 1
            r = rows[0]
            assert r["completed_count"] == 1
            assert r["timeout_count"] == 1
            assert r["aborted_count"] == 1
            assert r["cycle_count"] == 3
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_skips_in_flight_cycles(self):
        conn = await _setup_db()
        try:
            base = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
            # Open cycle (no ended_at) — must NOT be counted.
            log_ = CycleLog(
                id="open-1",
                thermostat_entity_id=THERMO_A,
                started_at=base,
                mode="cooling",
                rooms_json="{}",
            )
            await db.insert_cycle_log(conn, log_)

            day = base.date().isoformat()
            n = await db.rollup_daily_metrics(conn, day, day)
            assert n == 0
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_rerun_is_idempotent(self):
        conn = await _setup_db()
        try:
            base = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
            await _insert_finished_cycle(
                conn,
                cycle_id="i1",
                thermostat=THERMO_A,
                started_at=base,
                duration=timedelta(minutes=15),
            )
            day = base.date().isoformat()
            await db.rollup_daily_metrics(conn, day, day)
            await db.rollup_daily_metrics(conn, day, day)
            rows = await db.get_daily_thermostat_metrics(conn, start_date=day, end_date=day)
            assert len(rows) == 1
            assert rows[0]["cycle_count"] == 1
            assert rows[0]["cooling_seconds"] == 15 * 60
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_stale_rows_dropped_when_cycles_disappear(self):
        # If we wrote a row and then the underlying cycle was purged, a rerun
        # of the same window should drop the stale row.
        conn = await _setup_db()
        try:
            base = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
            await _insert_finished_cycle(
                conn,
                cycle_id="s1",
                thermostat=THERMO_A,
                started_at=base,
                duration=timedelta(minutes=15),
            )
            day = base.date().isoformat()
            await db.rollup_daily_metrics(conn, day, day)
            assert (
                len(await db.get_daily_thermostat_metrics(conn, start_date=day, end_date=day)) == 1
            )

            await conn.execute("DELETE FROM cycle_logs WHERE id='s1'")
            await conn.commit()
            await db.rollup_daily_metrics(conn, day, day)
            assert (
                len(await db.get_daily_thermostat_metrics(conn, start_date=day, end_date=day)) == 0
            )
        finally:
            await conn.close()


class TestMonthlyRollup:
    @pytest.mark.asyncio
    async def test_aggregates_across_a_month(self):
        conn = await _setup_db()
        try:
            base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
            await _insert_finished_cycle(
                conn,
                cycle_id="m1",
                thermostat=THERMO_A,
                started_at=base,
                duration=timedelta(minutes=30),
            )
            await _insert_finished_cycle(
                conn,
                cycle_id="m2",
                thermostat=THERMO_A,
                started_at=base + timedelta(days=15),
                duration=timedelta(minutes=45),
            )
            month = "2026-04"
            n = await db.rollup_monthly_metrics(conn, month, month)
            assert n == 1
            rows = await db.get_monthly_thermostat_metrics(conn, start_month=month, end_month=month)
            assert len(rows) == 1
            r = rows[0]
            assert r["cycle_count"] == 2
            assert r["cooling_seconds"] == (30 + 45) * 60
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------


def _make_ha() -> MagicMock:
    ha = MagicMock()
    ha.subscribe_all = MagicMock()
    ha.get_state.return_value = None
    ha.get_numeric_state.return_value = None
    ha.dev_mode = False
    return ha


class TestSchedulerRollup:
    @pytest.mark.asyncio
    async def test_run_daily_metrics_rollup_uses_local_today(self):
        sched = Scheduler(ha=_make_ha(), db_path=":memory:")
        sched._db_conn = await _setup_db()
        try:
            today = datetime.now()  # noqa: DTZ005 — match the scheduler's local-time call
            base = datetime(today.year, today.month, today.day, 12, 0, tzinfo=UTC)
            await _insert_finished_cycle(
                conn=sched._db_conn,
                cycle_id="t1",
                thermostat=THERMO_A,
                started_at=base,
                duration=timedelta(minutes=20),
            )
            n = await sched.run_daily_metrics_rollup(days_back=1)
            # At least the row for today; rows for "yesterday" only exist if
            # we happen to run across midnight, which we don't seed.
            assert n >= 1
            rows = await db.get_daily_thermostat_metrics(sched._db_conn)
            assert any(r["thermostat_entity_id"] == THERMO_A for r in rows)
        finally:
            await sched._db_conn.close()

    @pytest.mark.asyncio
    async def test_run_monthly_metrics_rollup(self):
        sched = Scheduler(ha=_make_ha(), db_path=":memory:")
        sched._db_conn = await _setup_db()
        try:
            today = datetime.now()  # noqa: DTZ005
            base = datetime(today.year, today.month, 1, 12, 0, tzinfo=UTC)
            await _insert_finished_cycle(
                conn=sched._db_conn,
                cycle_id="mr1",
                thermostat=THERMO_A,
                started_at=base,
                duration=timedelta(minutes=10),
            )
            n = await sched.run_monthly_metrics_rollup(months_back=1)
            assert n >= 1
        finally:
            await sched._db_conn.close()

    @pytest.mark.asyncio
    async def test_jobs_registered_after_start(self, tmp_path):
        # Spin a real scheduler against a file-backed DB so APScheduler
        # actually starts and the cron jobs become inspectable.
        ha = _make_ha()
        sched = Scheduler(ha=ha, db_path=str(tmp_path / "test.db"))
        await sched.start()
        try:
            ids = {j.id for j in sched._apscheduler.get_jobs()}
            assert "daily_metrics_rollup" in ids
            assert "monthly_metrics_rollup" in ids
        finally:
            await sched.stop()


# ---------------------------------------------------------------------------
# Coverage additions: read filters, timeseries guards, month math, and
# overshoot-histogram sample-quality edge cases
# ---------------------------------------------------------------------------


class TestMetricsReadFilters:
    async def test_daily_read_filters_by_thermostat(self):
        conn = await _setup_db()
        try:
            base = datetime(2026, 4, 13, 10, 0, tzinfo=UTC)
            await _insert_finished_cycle(
                conn,
                cycle_id="a1",
                thermostat=THERMO_A,
                started_at=base,
                duration=timedelta(minutes=20),
            )
            await _insert_finished_cycle(
                conn,
                cycle_id="b1",
                thermostat=THERMO_B,
                started_at=base,
                duration=timedelta(minutes=20),
            )
            await db.rollup_daily_metrics(conn, "2026-04-13", "2026-04-13")
            only_a = await db.get_daily_thermostat_metrics(conn, thermostat_entity_id=THERMO_A)
            assert {r["thermostat_entity_id"] for r in only_a} == {THERMO_A}
            both = await db.get_daily_thermostat_metrics(conn)
            assert {r["thermostat_entity_id"] for r in both} == {THERMO_A, THERMO_B}
        finally:
            await conn.close()

    async def test_monthly_read_filters_by_thermostat(self):
        conn = await _setup_db()
        try:
            base = datetime(2026, 4, 13, 10, 0, tzinfo=UTC)
            await _insert_finished_cycle(
                conn,
                cycle_id="a1",
                thermostat=THERMO_A,
                started_at=base,
                duration=timedelta(minutes=20),
            )
            await _insert_finished_cycle(
                conn,
                cycle_id="b1",
                thermostat=THERMO_B,
                started_at=base,
                duration=timedelta(minutes=20),
            )
            await db.rollup_monthly_metrics(conn, "2026-04", "2026-04")
            only_b = await db.get_monthly_thermostat_metrics(conn, thermostat_entity_id=THERMO_B)
            assert {r["thermostat_entity_id"] for r in only_b} == {THERMO_B}
        finally:
            await conn.close()


class TestSummaryMalformedRoomsJson:
    async def test_bad_rooms_json_is_skipped_not_fatal(self):
        conn = await _setup_db()
        try:
            base = datetime(2026, 4, 13, 10, 0, tzinfo=UTC)
            log_ = CycleLog(
                id="bad1",
                thermostat_entity_id=THERMO_A,
                started_at=base,
                mode="cooling",
                rooms_json="{not valid json",
            )
            await db.insert_cycle_log(conn, log_)
            await db.close_cycle_log(
                conn, "bad1", ended_at=base + timedelta(minutes=15), ended_reason="completed"
            )
            summary = await db.compute_thermostat_summary(
                conn, THERMO_A, "2026-04-13", "2026-04-13"
            )
            # The cycle still counts; the unparseable source breakdown is skipped.
            assert summary["cycle_count"] == 1
            assert summary["source_breakdown"] == {"schedule": 0, "presence": 0, "override": 0}
        finally:
            await conn.close()


class TestTimeseriesGuardsAndOutsideTemp:
    async def test_unsupported_granularity_raises(self):
        conn = await _setup_db()
        try:
            with pytest.raises(ValueError, match="unsupported granularity"):
                await db.compute_thermostat_timeseries(
                    conn, THERMO_A, "cycles", "week", "2026-04-01", "2026-04-30"
                )
        finally:
            await conn.close()

    async def test_outside_temp_metric_returns_avg_and_null(self):
        conn = await _setup_db()
        try:
            base = datetime(2026, 4, 13, 10, 0, tzinfo=UTC)
            await _insert_finished_cycle(
                conn,
                cycle_id="c1",
                thermostat=THERMO_A,
                started_at=base,
                duration=timedelta(minutes=20),
                outside_temp_at_start=50.0,
            )
            await _insert_finished_cycle(
                conn,
                cycle_id="c2",
                thermostat=THERMO_A,
                started_at=base + timedelta(hours=1),
                duration=timedelta(minutes=20),
                outside_temp_at_start=60.0,
            )
            # A day with no outside temp captured must surface as None, not 0.
            await _insert_finished_cycle(
                conn,
                cycle_id="c3",
                thermostat=THERMO_A,
                started_at=base + timedelta(days=1),
                duration=timedelta(minutes=20),
            )
            out = await db.compute_thermostat_timeseries(
                conn, THERMO_A, "outside_temp", "day", "2026-04-13", "2026-04-14"
            )
            by_period = {r["period"]: r["value"] for r in out}
            assert by_period["2026-04-13"] == pytest.approx(55.0)
            assert by_period["2026-04-14"] is None
        finally:
            await conn.close()


class TestMonthHelpers:
    def test_seconds_in_month_regular(self):
        assert db._seconds_in_month("2026-04") == 30 * 86400.0

    def test_seconds_in_month_december_wraps_year(self):
        assert db._seconds_in_month("2026-12") == 31 * 86400.0

    def test_seconds_in_month_february_leap(self):
        assert db._seconds_in_month("2028-02") == 29 * 86400.0

    def test_period_key_month(self):
        assert db._period_key(datetime(2026, 4, 13, 10, 0), "month") == "2026-04"
        assert db._period_key(datetime(2026, 4, 13, 10, 0), "day") == "2026-04-13"


class TestOvershootHistogramEdgeCases:
    async def _seed_cycle(self, conn, cycle_id: str, mode: str = "heating") -> datetime:
        from backend.models import Room

        await db.upsert_room(conn, Room(id="r1", name="R1", thermostat_entity_id=THERMO_A))
        base = datetime(2026, 4, 13, 10, 0, tzinfo=UTC)
        await _insert_finished_cycle(
            conn,
            cycle_id=cycle_id,
            thermostat=THERMO_A,
            started_at=base,
            duration=timedelta(minutes=30),
            mode=mode,
        )
        return base

    async def test_all_null_samples_produce_no_overshoot_entry(self):
        """A room whose every sample carried no temperature is counted as a
        participation but contributes nothing to the bins."""
        from backend.models import RoomCycleState

        conn = await _setup_db()
        try:
            base = await self._seed_cycle(conn, "c1")
            await db.upsert_room_cycle_state(
                conn, RoomCycleState(cycle_id="c1", room_id="r1", target_temp=74.0)
            )
            await db.insert_cycle_temp_sample(
                conn, "c1", "r1", base + timedelta(minutes=5), None, None, 74.0
            )
            hist = await db.compute_overshoot_histogram(conn, THERMO_A, "2026-04-13", "2026-04-13")
            assert hist["total_room_cycles"] == 1
            assert sum(hist["counts"]) == 0
            assert hist["overshot_count"] == 0
            assert hist["max_overshoot_f"] == 0.0
        finally:
            await conn.close()

    async def test_heating_overshoot_binned_with_thermostat_fallback(self):
        """A room with no room-level temps falls back to thermostat-level
        samples; the worst wrong-side excursion lands in the right bin."""
        from backend.models import RoomCycleState

        conn = await _setup_db()
        try:
            base = await self._seed_cycle(conn, "c1")
            await db.upsert_room_cycle_state(
                conn, RoomCycleState(cycle_id="c1", room_id="r1", target_temp=74.0)
            )
            # Thermostat-level samples only (room_id=None): peak 76.4 → 2.4°F over.
            await db.insert_cycle_temp_sample(
                conn, "c1", None, base + timedelta(minutes=5), None, 73.0, 74.0
            )
            await db.insert_cycle_temp_sample(
                conn, "c1", None, base + timedelta(minutes=20), None, 76.4, 74.0
            )
            hist = await db.compute_overshoot_histogram(conn, THERMO_A, "2026-04-13", "2026-04-13")
            assert hist["total_room_cycles"] == 1
            assert hist["overshot_count"] == 1
            assert hist["counts"][2] == 1  # 2.4 falls in the [2, 3) bin
            assert hist["max_overshoot_f"] == pytest.approx(2.4)
        finally:
            await conn.close()
