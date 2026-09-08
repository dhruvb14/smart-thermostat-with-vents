"""Metrics retention is separate from log retention (Issue #617).

The invariant this suite exists to hold: **no log-retention setting deletes
data that any metric reads. Only ``metrics_retention_days`` does.**

Before #617, ``_purge_old_logs`` hard-DELETEd ``cycle_logs`` on
``cycle_log_retention_days``. ``cycle_logs`` is the source of record for every
metric — ``compute_thermostat_summary``, the scatter, the overshoot histogram,
the hour heatmap, the vent timeline, all of it — and the delete cascades
(``ON DELETE CASCADE``) to ``room_cycle_states`` and ``cycle_vent_events``. So
shrinking "how much cycle history do I browse" silently erased every trend.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend import db
from backend.models import CycleLog, Room, RoomCycleState

THERMO = "climate.thermo_a"


async def _conn(client):
    return await client.app["scheduler"].get_db()


async def _seed(conn, *, cycle_id: str, started_at: datetime) -> None:
    await db.insert_cycle_log(
        conn,
        CycleLog(
            id=cycle_id,
            thermostat_entity_id=THERMO,
            started_at=started_at,
            mode="cooling",
            rooms_json="{}",
        ),
    )
    await db.close_cycle_log(
        conn,
        cycle_id,
        ended_at=started_at + timedelta(minutes=20),
        ended_reason="completed",
    )


def _noon(days_ago: int) -> datetime:
    return datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(
        days=days_ago
    )


def _local_date(days_ago: int) -> str:
    return (datetime.now().date() - timedelta(days=days_ago)).isoformat()  # noqa: DTZ005


async def _summary(client, *, days_back: int) -> dict:
    resp = await client.get(
        f"/api/metrics/thermostats/{THERMO}/summary"
        f"?start={_local_date(days_back)}&end={_local_date(0)}"
    )
    assert resp.status == 200
    data: dict = await resp.json()
    return data


# ---------------------------------------------------------------------------
# The write boundary
# ---------------------------------------------------------------------------


class TestLogRetentionEndpoint:
    @pytest.mark.asyncio
    async def test_get_reports_all_three_windows(self, client):
        data = await (await client.get("/api/settings/log-retention")).json()
        assert data == {
            "event_log_retention_days": 7,
            "cycle_log_retention_days": 30,
            "metrics_retention_days": 365,
        }

    @pytest.mark.asyncio
    async def test_round_trip_persists_metrics_retention(self, client):
        resp = await client.post("/api/settings/log-retention", json={"metrics_retention_days": 90})
        assert resp.status == 200
        assert (await resp.json())["metrics_retention_days"] == 90
        # The echo and the next GET agree — they share one reader.
        again = await (await client.get("/api/settings/log-retention")).json()
        assert again["metrics_retention_days"] == 90
        # And the siblings are untouched by a partial body.
        assert again["event_log_retention_days"] == 7
        assert again["cycle_log_retention_days"] == 30

    @pytest.mark.asyncio
    async def test_zero_is_accepted_as_keep_forever(self, client):
        """The siblings' ``max(1, int(v))`` would turn 0 into 1 — "keep
        forever" silently becoming "keep one day", deleting the archive the
        operator had just asked to protect."""
        resp = await client.post("/api/settings/log-retention", json={"metrics_retention_days": 0})
        assert resp.status == 200
        assert (await resp.json())["metrics_retention_days"] == 0

    @pytest.mark.asyncio
    async def test_negative_is_refused_rather_than_read_two_ways(self, client):
        """A negative used to be accepted and stored as 1 — "delete everything
        older than a day" — while ``db.coerce_metrics_retention_days`` reads a
        stored ``-1`` as 0, "keep forever". Two opposite readings of the same
        number, in a pair of helpers whose whole purpose is that they cannot
        disagree. ``-1`` is also the common idiom for "unlimited", so the caller
        reaching for "no limit" got the most destructive setting available,
        echoed back as a success. Refuse it instead of picking a reading."""
        resp = await client.post("/api/settings/log-retention", json={"metrics_retention_days": -5})
        assert resp.status == 400
        after = await (await client.get("/api/settings/log-retention")).json()
        assert after["metrics_retention_days"] == 365

    @pytest.mark.asyncio
    async def test_an_absurd_window_is_refused_rather_than_breaking_startup(self, client):
        """Every consumer subtracts these settings from today's date and
        ``timedelta`` overflows a little past 739,000 days. A 200 on
        ``999999999`` made ``_purge_old_logs`` raise OverflowError — and that
        coroutine is awaited unguarded inside ``Scheduler.start()``, so the
        add-on stopped booting and the UI needed to undo the value never came
        up. "0 = keep forever" makes a very large number the natural mis-guess
        for the same intent, so this is a plausible request, not a hostile
        one."""
        resp = await client.post(
            "/api/settings/log-retention", json={"metrics_retention_days": 999999999}
        )
        assert resp.status == 400
        assert str(db.MAX_RETENTION_DAYS) in (await resp.json())["error"]
        after = await (await client.get("/api/settings/log-retention")).json()
        assert after["metrics_retention_days"] == 365

    @pytest.mark.asyncio
    async def test_the_ceiling_itself_is_accepted(self, client):
        """The bound is inclusive — and a hundred years of retention still
        computes a floor rather than overflowing."""
        resp = await client.post(
            "/api/settings/log-retention",
            json={"metrics_retention_days": db.MAX_RETENTION_DAYS},
        )
        assert resp.status == 200
        assert (await resp.json())["metrics_retention_days"] == db.MAX_RETENTION_DAYS
        assert (await client.get("/api/logs")).status == 200
        assert (await _summary(client, days_back=1))["cycle_count"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "field",
        ["event_log_retention_days", "cycle_log_retention_days"],
    )
    async def test_the_sibling_windows_share_the_metrics_type_contract(self, client, field):
        """The siblings used to coerce a bool into a retention policy (``true``
        → 1 day, 200 OK) and to 500 on a non-numeric string out of a bare
        ``int("abc")``. One validation pass now covers all three, so the
        endpoint has one contract rather than three."""
        for bad in (True, "abc", 0, db.MAX_RETENTION_DAYS + 1):
            resp = await client.post("/api/settings/log-retention", json={field: bad})
            assert resp.status == 400, (field, bad)
            assert field in (await resp.json())["error"]
        after = await (await client.get("/api/settings/log-retention")).json()
        assert after["event_log_retention_days"] == 7
        assert after["cycle_log_retention_days"] == 30

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [True, False, "90", 90.5, None])
    async def test_non_integers_are_refused(self, client, bad):
        """Bools especially: ``isinstance(False, int)`` is True, so a JSON
        ``false`` would sail through ``int(...)`` as 0 and switch the install to
        keep-forever, and ``true`` as a one-day retention policy invented from a
        boolean — #609's exact defect, one field over."""
        resp = await client.post(
            "/api/settings/log-retention", json={"metrics_retention_days": bad}
        )
        assert resp.status == 400
        # Nothing was written.
        after = await (await client.get("/api/settings/log-retention")).json()
        assert after["metrics_retention_days"] == 365

    @pytest.mark.asyncio
    async def test_a_refused_metrics_value_does_not_half_apply_the_body(self, client):
        """The whole body is validated before any of it is written.

        The metrics arm used to be validated *last*, after its siblings had
        already committed, so this exact request answered 400 while quietly
        cutting event-log retention from 7 days to 1 — which the very next
        ``_purge_old_logs`` acted on irreversibly. This endpoint is MCP-exposed
        (``post_settings_log_retention``), so the caller reading that 400 as
        "nothing changed" and retrying is an agent as often as a person."""
        resp = await client.post(
            "/api/settings/log-retention",
            json={
                "event_log_retention_days": 1,
                "cycle_log_retention_days": 2,
                "metrics_retention_days": True,
            },
        )
        assert resp.status == 400
        assert "metrics_retention_days" in (await resp.json())["error"]
        after = await (await client.get("/api/settings/log-retention")).json()
        assert after == {
            "event_log_retention_days": 7,
            "cycle_log_retention_days": 30,
            "metrics_retention_days": 365,
        }

    @pytest.mark.asyncio
    async def test_a_valid_body_still_writes_every_field(self, client):
        """The mirror of the test above: validating up front must not have cost
        the endpoint its multi-field write."""
        resp = await client.post(
            "/api/settings/log-retention",
            json={
                "event_log_retention_days": 14,
                "cycle_log_retention_days": 60,
                "metrics_retention_days": 900,
            },
        )
        assert resp.status == 200
        assert await (await client.get("/api/settings/log-retention")).json() == {
            "event_log_retention_days": 14,
            "cycle_log_retention_days": 60,
            "metrics_retention_days": 900,
        }

    @pytest.mark.asyncio
    async def test_junk_stored_value_reads_back_as_the_default(self, client):
        conn = await _conn(client)
        await db.set_system_setting(conn, "metrics_retention_days", "three-sixty-five")
        data = await (await client.get("/api/settings/log-retention")).json()
        assert data["metrics_retention_days"] == 365


# ---------------------------------------------------------------------------
# The invariant: purging logs must not cost a metric
# ---------------------------------------------------------------------------


class TestPurgingLogsLeavesMetricsIntact:
    @pytest.mark.asyncio
    async def test_minimum_cycle_log_retention_costs_no_metric(self, client):
        """#617 AC1, the inverted reproduction. Before the fix this exact
        sequence took the summary from 1 cycle to 0 and left zero rows in
        ``cycle_logs``."""
        conn = await _conn(client)
        scheduler = client.app["scheduler"]
        scheduler._db_conn = conn
        await _seed(conn, cycle_id="c20", started_at=_noon(20))
        before = await _summary(client, days_back=29)
        assert before["cycle_count"] == 1

        await db.set_system_setting(conn, "cycle_log_retention_days", "1")
        await scheduler._purge_old_logs()

        after = await _summary(client, days_back=29)
        assert after["cycle_count"] == before["cycle_count"]
        assert after == before
        async with conn.execute("SELECT COUNT(*) AS n FROM cycle_logs") as cur:
            assert (await cur.fetchone())["n"] == 1

    @pytest.mark.asyncio
    async def test_minimum_event_log_retention_costs_no_metric(self, client):
        """#617 AC2. ``event_log`` is a genuine log — no metric reads it."""
        conn = await _conn(client)
        scheduler = client.app["scheduler"]
        scheduler._db_conn = conn
        await _seed(conn, cycle_id="c20", started_at=_noon(20))
        before = await _summary(client, days_back=29)

        await db.set_system_setting(conn, "event_log_retention_days", "1")
        await scheduler._purge_old_logs()

        assert await _summary(client, days_back=29) == before

    @pytest.mark.asyncio
    async def test_metrics_retention_is_what_deletes(self, client):
        """#617 AC3. The one setting that may — and the row is gone from the
        table, not merely outside the clamped read window.

        The summary assertion alone could not fail for the reason this test's
        name states: ``_parse_date_range`` clamps ``start`` forward to the
        metrics floor, so a 20-day-old cycle is outside the queried range
        whether or not the purge deleted anything. Disabling the purge entirely
        left this test green. The direct row counts — including the two
        ``ON DELETE CASCADE`` children, the bulk of what a purge destroys — are
        what actually observe the delete."""
        conn = await _conn(client)
        scheduler = client.app["scheduler"]
        scheduler._db_conn = conn
        await _seed(conn, cycle_id="c20", started_at=_noon(20))
        await db.upsert_room(
            conn, Room(id="room-a", name="Upstairs Office", thermostat_entity_id=THERMO)
        )
        await db.upsert_room_cycle_state(
            conn, RoomCycleState(cycle_id="c20", room_id="room-a", target_temp=70.0)
        )
        await db.insert_cycle_vent_event(
            conn, "c20", _noon(20), "cover.a_vent", "room-a", "opened_at_start"
        )
        await db.set_system_setting(conn, "metrics_retention_days", "10")

        await scheduler._purge_old_logs()

        for table in ("cycle_logs", "room_cycle_states", "cycle_vent_events"):
            async with conn.execute(f"SELECT COUNT(*) AS n FROM {table}") as cur:  # noqa: S608
                assert (await cur.fetchone())["n"] == 0, table
        assert (await _summary(client, days_back=29))["cycle_count"] == 0

    @pytest.mark.asyncio
    async def test_metrics_retention_zero_keeps_everything(self, client):
        """#617 AC4."""
        conn = await _conn(client)
        scheduler = client.app["scheduler"]
        scheduler._db_conn = conn
        await _seed(conn, cycle_id="ancient", started_at=_noon(2000))
        await db.set_system_setting(conn, "metrics_retention_days", "0")

        await scheduler._purge_old_logs()

        assert (await _summary(client, days_back=2100))["cycle_count"] == 1


# ---------------------------------------------------------------------------
# The Cycle History display window
# ---------------------------------------------------------------------------


class TestCycleHistoryDisplayWindow:
    @pytest.mark.asyncio
    async def test_rows_outside_the_window_are_hidden_but_still_counted(self, client):
        """#617 AC6. Absent from the list, present in the DB, counted by every
        metric — that is the whole difference between a display window and a
        purge."""
        conn = await _conn(client)
        await db.set_system_setting(conn, "cycle_log_retention_days", "5")
        await _seed(conn, cycle_id="recent", started_at=_noon(2))
        await _seed(conn, cycle_id="older", started_at=_noon(40))

        listing = await (await client.get("/api/logs")).json()
        assert {c["id"] for c in listing} == {"recent"}

        async with conn.execute("SELECT COUNT(*) AS n FROM cycle_logs") as cur:
            assert (await cur.fetchone())["n"] == 2
        assert (await _summary(client, days_back=60))["cycle_count"] == 2

    @pytest.mark.asyncio
    async def test_widening_start_cannot_escape_the_window(self, client):
        conn = await _conn(client)
        await db.set_system_setting(conn, "cycle_log_retention_days", "5")
        await _seed(conn, cycle_id="recent", started_at=_noon(2))
        await _seed(conn, cycle_id="older", started_at=_noon(40))

        listing = await (await client.get(f"/api/logs?start={_local_date(90)}")).json()
        assert {c["id"] for c in listing} == {"recent"}

    @pytest.mark.asyncio
    async def test_a_window_wider_than_metrics_retention_shows_what_is_retained(self, client):
        """A display window is a maximum, not a promise. Asking to browse 90
        days over a 10-day metrics window shows 10 days of data, not 80 days of
        rows that no longer exist."""
        conn = await _conn(client)
        await db.set_system_setting(conn, "cycle_log_retention_days", "90")
        await db.set_system_setting(conn, "metrics_retention_days", "10")
        await _seed(conn, cycle_id="recent", started_at=_noon(2))
        # Still physically present — the purge is lazy (daily) — but outside the
        # retained window, so the listing must not advertise it.
        await _seed(conn, cycle_id="older", started_at=_noon(40))

        listing = await (await client.get("/api/logs")).json()
        assert {c["id"] for c in listing} == {"recent"}

    @pytest.mark.asyncio
    async def test_keep_forever_lists_the_whole_archive_within_the_window(self, client):
        """``metrics_retention_days=0`` removes the retention floor entirely, so
        the display window is the only bound left."""
        conn = await _conn(client)
        await db.set_system_setting(conn, "cycle_log_retention_days", "3650")
        await db.set_system_setting(conn, "metrics_retention_days", "0")
        await _seed(conn, cycle_id="ancient", started_at=_noon(2000))

        listing = await (await client.get("/api/logs")).json()
        assert {c["id"] for c in listing} == {"ancient"}

    @pytest.mark.asyncio
    async def test_demo_rows_stay_listed_outside_every_window(self, client):
        """The ``demo-`` dataset (#442) is exempt from the purge and lives in a
        fixed past window far outside any sane display window. Both floors have
        to make room for it or the Cycle History goldens — which pin the page to
        that week — come back empty."""
        conn = await _conn(client)
        await _seed(conn, cycle_id="demo-old", started_at=datetime(2025, 6, 2, 12, 0, tzinfo=UTC))

        listing = await (await client.get("/api/logs")).json()
        assert {c["id"] for c in listing} == {"demo-old"}
