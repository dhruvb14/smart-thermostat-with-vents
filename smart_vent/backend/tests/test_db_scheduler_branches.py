"""Branch-coverage tests for the untaken halves of guards in ``backend/db.py``
and ``backend/scheduler.py``.

Both modules had 100% *statement* coverage before branch coverage was enabled,
which hid a set of guards whose False path had never run: "nothing to stamp",
"no rows to migrate", "the DELETE matched nothing", "no broadcast wired",
"no DB connection", "no engine for this thermostat". Each test below asserts
the *behaviour* of the untaken path — the return value and the resulting DB or
scheduler state — not merely that the line executed.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import aiosqlite

from backend import db
from backend.models import Room, RoomOverride, RoomPresenceSensor
from backend.scheduler import Scheduler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fresh_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    return conn


async def _bare_conn() -> aiosqlite.Connection:
    """A connection with no application schema at all."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    return conn


async def _add_room(conn: aiosqlite.Connection, room_id: str, thermo: str, **kw) -> Room:
    room = Room(id=room_id, name=room_id.upper(), thermostat_entity_id=thermo, **kw)
    await db.upsert_room(conn, room)
    return room


async def _insert_cycle(
    conn: aiosqlite.Connection,
    cycle_id: str,
    thermo: str,
    started_at: str,
    ended_at: str | None,
    *,
    mode: str = "heating",
    rooms_json: str = "{}",
) -> None:
    await conn.execute(
        "INSERT INTO cycle_logs (id, thermostat_entity_id, started_at, ended_at, mode, rooms_json)"
        " VALUES (?,?,?,?,?,?)",
        (cycle_id, thermo, started_at, ended_at, mode, rooms_json),
    )
    await conn.commit()


def _make_ha() -> MagicMock:
    ha = MagicMock()
    ha.subscribe_all = MagicMock()
    ha.get_state.return_value = {"state": "off", "attributes": {}}
    ha.get_numeric_state.return_value = None
    ha.set_thermostat_temperature = AsyncMock()
    ha.dev_mode = False
    ha._dev_logger = None
    return ha


def _make_scheduler(ha: MagicMock | None = None) -> Scheduler:
    sched = Scheduler(ha=ha or _make_ha(), db_path=":memory:")
    # The real AsyncIOScheduler raises if shut down while never started, and
    # none of these tests exercise APScheduler itself.
    sched._apscheduler = MagicMock()
    sched._vent_ctrl = MagicMock()
    return sched


# ---------------------------------------------------------------------------
# db.py — _stamp_baseline (470->472: `if stamped:` False)
# ---------------------------------------------------------------------------


class TestStampBaselineNothingAdopted:
    async def test_empty_database_stamps_nothing(self):
        """A DB with no application tables has no migration effect already in
        place, so baseline adoption stamps nothing and reports an empty set —
        every migration stays genuinely pending."""
        conn = await _bare_conn()
        try:
            await conn.execute(
                """CREATE TABLE schema_migrations (
                       version INTEGER PRIMARY KEY,
                       applied_at TEXT NOT NULL DEFAULT (datetime('now')),
                       description TEXT NOT NULL
                   )"""
            )
            await conn.commit()

            stamped = await db._stamp_baseline(conn)

            assert stamped == set()
            async with conn.execute("SELECT COUNT(*) FROM schema_migrations") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == 0
        finally:
            await conn.close()

    async def test_fully_migrated_database_stamps_every_migration(self):
        """Contrast case (the already-covered True arm): the full SCHEMA
        snapshot has every migration's effect in place, so all are adopted."""
        conn = await _fresh_conn()
        try:
            await conn.execute("DELETE FROM schema_migrations")
            await conn.commit()

            stamped = await db._stamp_baseline(conn)

            assert stamped == {m.version for m in db.MIGRATIONS}
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# db.py — _migrate_holdover_timestamps_to_utc (615->620: `if rows:` False)
# ---------------------------------------------------------------------------


class TestHoldoverUtcMigrationNoRows:
    async def test_non_utc_server_with_no_holdovers_still_stamps_sentinel(self):
        """The offset is non-zero (so the shift block runs) but there is
        nothing to shift: the migration must still commit and stamp its
        sentinel, leaving the table empty and never running again."""
        conn = await _fresh_conn()
        try:
            await conn.execute(
                "DELETE FROM system_settings WHERE key='migration_holdover_timestamps_utc_v1'"
            )
            await conn.commit()
            async with conn.execute("SELECT COUNT(*) FROM presence_holdover_state") as cur:
                row = await cur.fetchone()
            assert row is not None and row[0] == 0

            os.environ["TZ"] = "Etc/GMT+5"  # UTC-5, no DST → offset always >= 1s
            time.tzset()
            try:
                await db._migrate_holdover_timestamps_to_utc(conn)
            finally:
                os.environ["TZ"] = "UTC"
                time.tzset()

            assert (
                await db.get_system_setting(conn, "migration_holdover_timestamps_utc_v1", "missing")
                == "1"
            )
            async with conn.execute("SELECT COUNT(*) FROM presence_holdover_state") as cur:
                row = await cur.fetchone()
            assert row is not None and row[0] == 0
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# db.py — clear_expired_overrides (1596->1591: `if cur2.rowcount:` False)
# ---------------------------------------------------------------------------


class _RepostOtherHoldOnFirstDelete:
    """Connection proxy that re-posts the *other* expired hold with a future
    expiry just before the first per-row DELETE runs.

    This is the race ``clear_expired_overrides`` documents: engines tick
    concurrently, so a hold can be re-posted between the SELECT snapshot and
    the DELETE. Driving it through a proxy makes the interleaving exact
    instead of depending on task scheduling.
    """

    def __init__(self, real: aiosqlite.Connection, room_ids: list[str], new_expires: str) -> None:
        self._real = real
        self._room_ids = room_ids
        self._new_expires = new_expires
        self._fired = False
        self.reposted: str | None = None

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    def execute(self, sql: str, parameters=None):
        if sql.lstrip().upper().startswith("DELETE") and not self._fired:
            self._fired = True
            return self._delete_with_repost(sql, parameters)
        return self._real.execute(sql, parameters)

    async def _delete_with_repost(self, sql: str, parameters):
        victim = next(r for r in self._room_ids if r != parameters[0])
        self.reposted = victim
        await self._real.execute(
            "UPDATE room_overrides SET expires_at=? WHERE room_id=?",
            (self._new_expires, victim),
        )
        return await self._real.execute(sql, parameters)


class TestClearExpiredOverridesNoRowDeleted:
    async def test_hold_reposted_between_select_and_delete_is_not_reported(self):
        past = datetime(2020, 1, 1, 0, 0, 0)
        future = datetime(2099, 1, 1, 0, 0, 0)
        conn = await _fresh_conn()
        try:
            await _add_room(conn, "r1", "climate.a")
            await _add_room(conn, "r2", "climate.a")
            for rid in ("r1", "r2"):
                await db.set_room_override(
                    conn, RoomOverride(room_id=rid, target_temp=70.0, expires_at=past)
                )

            proxy = _RepostOtherHoldOnFirstDelete(conn, ["r1", "r2"], future.isoformat())
            expired = await db.clear_expired_overrides(proxy)

            # Only the hold whose DELETE actually removed a row is reported.
            assert [o.room_id for o in expired] == [r for r in ("r1", "r2") if r != proxy.reposted]
            # The re-posted hold survives untouched with its new expiry.
            surviving = await db.get_room_override(conn, proxy.reposted or "")
            assert surviving is not None
            assert surviving.expires_at.replace(tzinfo=None) == future
            async with conn.execute("SELECT COUNT(*) FROM room_overrides") as cur:
                row = await cur.fetchone()
            assert row is not None and row[0] == 1
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# db.py — cycle-log closers
#   1708->1710 : close_open_cycle_logs   `if ended_at is None` False
#   1736->1738 : close_open_cycles_for_rooms `if ended_at is None` False
#   1752->1755 : close_open_cycles_for_rooms `if exclude_thermostat:` False
# ---------------------------------------------------------------------------


class TestCycleLogClosersExplicitEndedAt:
    async def test_close_open_cycle_logs_honours_caller_supplied_timestamp(self):
        conn = await _fresh_conn()
        try:
            stamp = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)
            await _insert_cycle(conn, "c1", "climate.a", "2025-03-04T04:00:00", None)
            await _insert_cycle(conn, "c2", "climate.a", "2025-03-04T04:30:00", None)
            await _insert_cycle(conn, "c3", "climate.b", "2025-03-04T04:40:00", None)

            closed = await db.close_open_cycle_logs(conn, "climate.a", ended_at=stamp)

            assert closed == 2
            async with conn.execute("SELECT id, ended_at FROM cycle_logs ORDER BY id") as cur:
                rows = {r["id"]: r["ended_at"] for r in await cur.fetchall()}
            assert rows["c1"] == "2025-03-04T05:06:07"
            assert rows["c2"] == "2025-03-04T05:06:07"
            assert rows["c3"] is None  # other thermostat untouched
        finally:
            await conn.close()

    async def test_close_open_cycles_for_rooms_without_exclusion_closes_every_thermostat(self):
        """No ``exclude_thermostat`` → the query carries no thermostat filter,
        so a room's open cycle is closed on *every* thermostat, at the exact
        caller-supplied timestamp."""
        conn = await _fresh_conn()
        try:
            stamp = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)
            await _add_room(conn, "r1", "climate.a")
            await _insert_cycle(conn, "c1", "climate.a", "2025-03-04T04:00:00", None)
            await _insert_cycle(conn, "c2", "climate.b", "2025-03-04T04:00:00", None)
            for cid in ("c1", "c2"):
                await conn.execute(
                    "INSERT INTO room_cycle_states (cycle_id, room_id, target_temp, role)"
                    " VALUES (?,?,?,'active')",
                    (cid, "r1", 70.0),
                )
            await conn.commit()

            closed = await db.close_open_cycles_for_rooms(conn, ["r1"], ended_at=stamp)

            assert closed == 2
            async with conn.execute("SELECT id, ended_at FROM cycle_logs ORDER BY id") as cur:
                rows = {r["id"]: r["ended_at"] for r in await cur.fetchall()}
            assert rows["c1"] == "2025-03-04T05:06:07"
            assert rows["c2"] == "2025-03-04T05:06:07"
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# db.py — compute_thermostat_summary source breakdown (2463->2457)
# ---------------------------------------------------------------------------


class TestSummarySourceBreakdown:
    async def test_missing_and_duplicate_sources_are_not_counted(self):
        """A source counts once per cycle: the second room carrying the same
        source, a room with no ``source`` key, and a null room entry all fall
        through the guard without incrementing anything."""
        conn = await _fresh_conn()
        try:
            rooms_json = json.dumps(
                {
                    "a": {"source": "schedule"},
                    "b": {"source": "schedule"},  # duplicate → not recounted
                    "c": {"target": 70.0},  # no source key
                    "d": None,  # null room entry
                }
            )
            await _insert_cycle(
                conn,
                "c1",
                "climate.a",
                "2025-06-02T10:00:00",
                "2025-06-02T10:30:00",
                rooms_json=rooms_json,
            )

            summary = await db.compute_thermostat_summary(
                conn, "climate.a", "2025-06-02", "2025-06-02"
            )

            assert summary["source_breakdown"] == {"schedule": 1, "presence": 0, "override": 0}
        finally:
            await conn.close()

    async def test_a_non_object_snapshot_costs_only_its_own_cycle(self):
        """#604: the decode guard was only half the guard here too. A snapshot
        that is valid JSON but not an object (a hand-edited backup restored
        through /api/restore) raised ``AttributeError`` on ``.values()``, which
        escapes ``compute_thermostat_summary`` entirely — so one unreadable row
        took out the Metrics source breakdown for the whole date range, not
        just its own cycle. The unreadable row must be skipped and its readable
        neighbour still counted."""
        conn = await _fresh_conn()
        try:
            await _insert_cycle(
                conn,
                "c-bad",
                "climate.a",
                "2025-06-02T10:00:00",
                "2025-06-02T10:30:00",
                rooms_json='["r1"]',
            )
            await _insert_cycle(
                conn,
                "c-good",
                "climate.a",
                "2025-06-02T11:00:00",
                "2025-06-02T11:30:00",
                rooms_json=json.dumps({"a": {"source": "override"}}),
            )

            summary = await db.compute_thermostat_summary(
                conn, "climate.a", "2025-06-02", "2025-06-02"
            )

            assert summary["cycle_count"] == 2, "both cycles still count as cycles"
            assert summary["source_breakdown"] == {"schedule": 0, "presence": 0, "override": 1}
        finally:
            await conn.close()

    async def test_unreadable_room_entries_and_sources_are_skipped(self):
        """#604, per-entry: a room entry that is not an object, and a
        ``source`` that is not a string, are both unreadable. The unhashable
        case matters most — a list ``source`` would raise ``TypeError`` on the
        ``in seen`` membership test, with the same range-wide blast radius."""
        conn = await _fresh_conn()
        try:
            rooms_json = json.dumps(
                {
                    "a": 74.0,  # entry is not an object
                    "b": {"source": 5},  # source is not a string
                    "c": {"source": ["schedule"]},  # ...and is unhashable
                    "d": {"source": ""},  # empty string is not a source
                    "e": {"source": "presence"},  # the one readable entry
                }
            )
            await _insert_cycle(
                conn,
                "c1",
                "climate.a",
                "2025-06-02T10:00:00",
                "2025-06-02T10:30:00",
                rooms_json=rooms_json,
            )

            summary = await db.compute_thermostat_summary(
                conn, "climate.a", "2025-06-02", "2025-06-02"
            )

            assert summary["source_breakdown"] == {"schedule": 0, "presence": 1, "override": 0}
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# Not covered on purpose: db.py 2634->2594, the False arm of the final
# ``elif metric == "short_cycles"`` in ``compute_thermostat_timeseries``.
# ``metric`` is validated against a fixed 8-value tuple at the top of the
# function, and two of those eight ("time_to_target", "degree_minutes")
# return before the row loop is reached. The six that do reach it are exactly
# the six the if/elif chain names, so control can only arrive at the last
# ``elif`` when the metric IS "short_cycles" — its False arm is unreachable
# without changing the source. Please do not "fix" it with a pragma.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# db.py — _degree_minutes_timeseries tail flush (2763->2748)
# ---------------------------------------------------------------------------


class TestDegreeMinutesTailFlush:
    async def test_no_tail_when_last_sample_is_not_before_cycle_end(self):
        """A sample stamped at or after ``ended_at`` (the sampler ticked as the
        cycle was being closed) must contribute no tail — without the guard the
        negative interval would *subtract* degree-minutes from the bucket."""
        conn = await _fresh_conn()
        try:
            await _insert_cycle(
                conn, "c1", "climate.a", "2025-06-02T10:00:00", "2025-06-02T10:10:00"
            )
            for ts, tstat, sp in (
                ("2025-06-02T10:00:00", 68.0, 70.0),  # delta 2 for 10 minutes
                ("2025-06-02T10:12:00", 69.0, 70.0),  # past ended_at → no tail
            ):
                await conn.execute(
                    "INSERT INTO cycle_temp_samples (cycle_id, room_id, timestamp,"
                    " room_temp, thermostat_temp, setpoint) VALUES (?,?,?,?,?,?)",
                    ("c1", "r1", ts, None, tstat, sp),
                )
            await conn.commit()

            series = await db.compute_thermostat_timeseries(
                conn, "climate.a", "degree_minutes", "day", "2025-06-02", "2025-06-02"
            )

            # 2 °F × 10 min only (the second sample's interval is clamped to
            # the cycle end and its own tail is dropped).
            assert series == [{"period": "2025-06-02", "value": 20.0}]
        finally:
            await conn.close()

    async def test_tail_is_flushed_when_cycle_outlasts_its_last_sample(self):
        """Contrast case (the True arm): the last sample precedes ``ended_at``
        so its delta is integrated to the cycle end."""
        conn = await _fresh_conn()
        try:
            await _insert_cycle(
                conn, "c1", "climate.a", "2025-06-02T10:00:00", "2025-06-02T10:15:00"
            )
            for ts, tstat, sp in (
                ("2025-06-02T10:00:00", 68.0, 70.0),  # delta 2 × 10 min
                ("2025-06-02T10:10:00", 69.0, 70.0),  # delta 1 × 5 min tail
            ):
                await conn.execute(
                    "INSERT INTO cycle_temp_samples (cycle_id, room_id, timestamp,"
                    " room_temp, thermostat_temp, setpoint) VALUES (?,?,?,?,?,?)",
                    ("c1", "r1", ts, None, tstat, sp),
                )
            await conn.commit()

            series = await db.compute_thermostat_timeseries(
                conn, "climate.a", "degree_minutes", "day", "2025-06-02", "2025-06-02"
            )

            assert series == [{"period": "2025-06-02", "value": 25.0}]
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# db.py — compute_hour_heatmap (3207->3210: `if secs:` False)
# ---------------------------------------------------------------------------


class TestHourHeatmapZeroSecondSlot:
    async def test_sub_second_trailing_slot_adds_nothing(self):
        """A cycle ending a fraction of a second into the next hour still
        enters that hour's slot, but truncates to zero seconds — the cell must
        stay empty rather than gaining a spurious 0-second entry."""
        conn = await _fresh_conn()
        try:
            # 2025-06-02 is a Monday; conftest pins TZ=UTC so local == stored.
            await _insert_cycle(
                conn,
                "c1",
                "climate.a",
                "2025-06-02T10:00:00",
                "2025-06-02T11:00:00.500000",
            )

            out = await db.compute_hour_heatmap(conn, "climate.a", "2025-06-02", "2025-06-02")

            grid = out["grid_seconds"]
            assert grid[0][10] == 3600
            assert grid[0][11] == 0
            assert sum(sum(hour_row) for hour_row in grid) == 3600
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# scheduler.py — stop / reload_db without a DB connection (218->220, 236->244)
# ---------------------------------------------------------------------------


class TestSchedulerLifecycleWithoutConnection:
    async def test_stop_before_start_does_not_touch_a_connection(self):
        sched = _make_scheduler()
        assert sched._db_conn is None

        await sched.stop()

        sched._apscheduler.shutdown.assert_called_once_with(wait=False)
        assert sched._db_conn is None

    async def test_reload_db_without_prior_connection_opens_one(self):
        """``reload_db`` before ``start`` has no engines to abort and no
        connection to close — it just opens and initialises a fresh one."""
        sched = _make_scheduler()
        assert sched._db_conn is None

        await sched.reload_db()

        try:
            assert sched._db_conn is not None
            assert sched._engines == {}
            # The new connection is a live, initialised DB.
            assert await db.get_system_setting(sched._db_conn, "system_enabled", "1") == "1"
        finally:
            await sched._db_conn.close()

    async def test_reload_db_keeps_env_forced_unit(self):
        """With TEMPERATURE_UNIT locked by env/add-on config the DB value is
        never consulted on reload — the override stays authoritative."""
        sched = _make_scheduler()
        await sched.reload_db()
        try:
            await db.set_system_setting(sched._db_conn, "temperature_unit", "F")
            sched._unit_override = "C"
            sched._active_unit = "C"

            await sched.reload_db()

            assert sched.get_temperature_unit() == "C"
        finally:
            await sched._db_conn.close()


# ---------------------------------------------------------------------------
# scheduler.py — toggles with no broadcast wired (312, 330, 343)
# ---------------------------------------------------------------------------


class TestTogglesWithoutBroadcast:
    async def test_set_mcp_enabled_persists_without_broadcast(self):
        sched = _make_scheduler()
        assert sched._broadcast is None
        conn = await _fresh_conn()
        sched._db_conn = conn
        try:
            await sched.set_mcp_enabled(True)

            assert sched.get_mcp_enabled() is True
            assert await db.get_system_setting(conn, "mcp_enabled", "0") == "1"
        finally:
            await conn.close()

    async def test_set_mqtt_enabled_persists_without_broadcast(self):
        sched = _make_scheduler()
        conn = await _fresh_conn()
        sched._db_conn = conn
        try:
            await sched.set_mqtt_enabled(True)

            assert sched.get_mqtt_enabled() is True
            assert await db.get_system_setting(conn, "mqtt_enabled", "0") == "1"
        finally:
            await conn.close()

    async def test_set_theme_persists_without_broadcast(self):
        sched = _make_scheduler()
        conn = await _fresh_conn()
        sched._db_conn = conn
        try:
            await sched.set_theme("dark")

            assert sched.get_theme() == "dark"
            assert await db.get_system_setting(conn, "theme", "system") == "dark"
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# scheduler.py — _refresh_continuous_presence re-arm without event logger (828)
# ---------------------------------------------------------------------------


class TestPresenceRearmWithoutEventLogger:
    async def test_empty_room_rearms_and_skips_holdover_refresh(self):
        ha = _make_ha()
        ha.get_state.return_value = {"state": "off", "attributes": {}}
        sched = _make_scheduler(ha)
        assert sched._event_logger is None
        conn = await _fresh_conn()
        sched._db_conn = conn
        try:
            await _add_room(conn, "r1", "climate.a", presence_holdover_hours=2.0)
            await db.add_room_presence_sensor(
                conn, RoomPresenceSensor.create("r1", "binary_sensor.occ")
            )
            await db.set_presence_suppression(conn, "r1", datetime(2025, 6, 2, 10, 0, tzinfo=UTC))
            engine = MagicMock()
            engine.handle_presence = AsyncMock()
            sched._engines["climate.a"] = engine

            await sched._refresh_continuous_presence()

            # Suppression is lifted (room emptied) but presence is NOT
            # resurrected on this pass — the next genuine occupancy does that.
            assert await db.is_presence_suppressed(conn, "r1") is False
            engine.handle_presence.assert_not_awaited()
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# scheduler.py — _handle_presence_event loop arms (944->943, 965->943)
# ---------------------------------------------------------------------------


class TestHandlePresenceEventLoop:
    async def test_non_matching_sensor_is_skipped(self):
        sched = _make_scheduler()
        conn = await _fresh_conn()
        sched._db_conn = conn
        try:
            await _add_room(conn, "r1", "climate.a")
            await db.add_room_presence_sensor(
                conn, RoomPresenceSensor.create("r1", "binary_sensor.other")
            )
            engine = MagicMock()
            engine.handle_presence = AsyncMock()
            sched._engines["climate.a"] = engine

            await sched._handle_presence_event("binary_sensor.unrelated")

            engine.handle_presence.assert_not_awaited()
        finally:
            await conn.close()

    async def test_matching_sensor_without_engine_is_a_noop(self):
        """The room's thermostat has no engine yet (rooms exist but
        ``_sync_engines`` has not run) — the event must be dropped quietly
        rather than raising."""
        sched = _make_scheduler()
        conn = await _fresh_conn()
        sched._db_conn = conn
        try:
            await _add_room(conn, "r1", "climate.a")
            await db.add_room_presence_sensor(
                conn, RoomPresenceSensor.create("r1", "binary_sensor.occ")
            )
            other = MagicMock()
            other.handle_presence = AsyncMock()
            sched._engines["climate.other"] = other

            await sched._handle_presence_event("binary_sensor.occ")

            assert sched._engines == {"climate.other": other}
            other.handle_presence.assert_not_awaited()
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# scheduler.py — kick_thermostat for an unknown thermostat (937->exit)
# ---------------------------------------------------------------------------


class TestKickUnknownThermostat:
    async def test_unknown_thermostat_ticks_nothing(self):
        sched = _make_scheduler()
        sched._engines["climate.a"] = MagicMock()
        tick = AsyncMock()
        sched._tick_engine = tick

        await sched.kick_thermostat("climate.nope")

        tick.assert_not_awaited()

    async def test_known_thermostat_ticks_once(self):
        """Contrast case (the True arm) so the no-op above is meaningful."""
        sched = _make_scheduler()
        engine = MagicMock()
        sched._engines["climate.a"] = engine
        tick = AsyncMock()
        sched._tick_engine = tick

        await sched.kick_thermostat("climate.a")

        tick.assert_awaited_once_with("climate.a", engine)
