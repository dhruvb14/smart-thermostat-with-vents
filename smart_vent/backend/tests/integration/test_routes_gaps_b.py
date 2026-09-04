"""Behavioural coverage for the defensive / error branches in the back half of
``backend/api/routes.py`` (statement numbers >= 2000).

Everything here is driven through the public HTTP surface. The recurring theme
is "the DB or Home Assistant handed us something malformed" — the routes are
written to degrade gracefully rather than 500, and each of those fallbacks is
pinned here with an assertion on the *observable* response, not on a mock call.

Where a handler wraps a call in ``except Exception``, the test also asserts the
response body carries no exception text (CLAUDE.md's CWE-209 rule / security
alert #4).
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3 as _sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from backend import db, demo_seed
from backend.models import RoomCycleState

THERMO = "climate.gap_thermo"


async def _conn(client):
    return await client.app["scheduler"].get_db()


def _noon_utc(days_ago: int = 0) -> datetime:
    return datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(
        days=days_ago
    )


def _local_date(days_ago: int = 0) -> str:
    return (datetime.now().date() - timedelta(days=days_ago)).isoformat()  # noqa: DTZ005


async def _register_thermostat(client, entity_id: str = THERMO) -> None:
    resp = await client.post(
        "/api/thermostats",
        json={"thermostat_entity_id": entity_id, "name": "Gap", "total_vents_count": 2},
    )
    assert resp.status == 201, await resp.text()


# ---------------------------------------------------------------------------
# GET /api/ha/entities with no `domain` filter (line 2215)
# ---------------------------------------------------------------------------


class TestHaEntitiesNoDomain:
    """Without ``domain`` the route enumerates the HA client's whole state
    cache. ``FakeHomeAssistant`` keeps its states in ``_state``; alias it onto
    ``_state_cache`` (the attribute name the real ``HAClient`` uses) so the
    no-domain branch runs against a realistic cache."""

    @staticmethod
    def _alias_cache(fake_ha) -> None:
        fake_ha._state_cache = fake_ha._state

    @pytest.mark.asyncio
    async def test_no_domain_returns_every_cached_entity_sorted(self, client, fake_ha):
        fake_ha.seed_state("sensor.zulu", "1", {"friendly_name": "Zulu"})
        fake_ha.seed_state("climate.alpha", "cool", {})
        self._alias_cache(fake_ha)

        resp = await client.get("/api/ha/entities")
        assert resp.status == 200
        body = await resp.json()
        ids = [e["entity_id"] for e in body]
        # Both domains present (no filtering) and sorted by entity_id.
        assert "sensor.zulu" in ids
        assert "climate.alpha" in ids
        assert ids == sorted(ids)
        by_id = {e["entity_id"]: e for e in body}
        assert by_id["sensor.zulu"]["friendly_name"] == "Zulu"
        assert by_id["sensor.zulu"]["state"] == "1"
        # No friendly_name attribute → falls back to the entity_id.
        assert by_id["climate.alpha"]["friendly_name"] == "climate.alpha"

    @pytest.mark.asyncio
    async def test_no_domain_still_applies_attribute_and_icon_filters(self, client, fake_ha):
        fake_ha.seed_state("climate.with_action", "cool", {"hvac_action": "cooling"})
        fake_ha.seed_state("climate.without_action", "cool", {})
        fake_ha.seed_state("cover.door", "open", {"hvac_action": "idle", "icon": "mdi:door-open"})
        self._alias_cache(fake_ha)

        resp = await client.get("/api/ha/entities?has_attribute=hvac_action")
        ids = {e["entity_id"] for e in await resp.json()}
        assert ids == {"climate.with_action", "cover.door"}

        resp = await client.get(
            "/api/ha/entities?has_attribute=hvac_action&exclude_icon=mdi:door-open"
        )
        ids = {e["entity_id"] for e in await resp.json()}
        assert ids == {"climate.with_action"}


# ---------------------------------------------------------------------------
# Malformed JSON columns on the cycle-log surfaces (2237-2246, 2375-2376,
# 2386-2387)
# ---------------------------------------------------------------------------


async def _insert_raw_cycle(
    conn,
    *,
    cycle_id: str,
    rooms_json: str,
    vents_at_start: str | None = None,
    vents_at_end: str | None = None,
    started_at: datetime | None = None,
    ended_at: str | None = None,
) -> None:
    """Insert a cycle_logs row directly so deliberately corrupt JSON survives
    (``db.insert_cycle_log`` takes already-serialised strings, but going
    straight to SQL keeps the intent obvious)."""
    started = (started_at or _noon_utc(0)).replace(tzinfo=None).isoformat()
    await conn.execute(
        """INSERT INTO cycle_logs(
            id, thermostat_entity_id, started_at, ended_at, mode, rooms_json,
            ended_reason, vents_at_start, vents_at_end
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            cycle_id,
            THERMO,
            started,
            ended_at
            if ended_at is not None
            else (started_at or _noon_utc(0)).replace(tzinfo=None).isoformat(),
            "cooling",
            rooms_json,
            "completed",
            vents_at_start,
            vents_at_end,
        ),
    )
    await conn.commit()


class TestCorruptCycleJsonDegradesGracefully:
    @pytest.mark.asyncio
    async def test_logs_list_falls_back_to_empty_rooms_and_null_vents(self, client):
        conn = await _conn(client)
        await _insert_raw_cycle(
            conn,
            cycle_id="corrupt-1",
            rooms_json="{not json",
            vents_at_start="[[[",
            vents_at_end="}}}",
        )
        # A well-formed neighbour proves the corrupt row didn't poison the list.
        await _insert_raw_cycle(
            conn,
            cycle_id="good-1",
            rooms_json=json.dumps({"r1": {"name": "Study"}}),
            vents_at_start=json.dumps({"cover.a": 100}),
            vents_at_end=json.dumps({"cover.a": 0}),
        )

        resp = await client.get("/api/logs")
        assert resp.status == 200
        by_id = {c["id"]: c for c in await resp.json()}

        bad = by_id["corrupt-1"]
        assert bad["rooms"] == {}
        assert bad["vents_at_start"] is None
        assert bad["vents_at_end"] is None

        good = by_id["good-1"]
        assert good["rooms"] == {"r1": {"name": "Study"}}
        assert good["vents_at_start"] == {"cover.a": 100}
        assert good["vents_at_end"] == {"cover.a": 0}

    @pytest.mark.asyncio
    async def test_detail_falls_back_for_rooms_json_and_trigger_detail(self, client):
        conn = await _conn(client)
        await _insert_raw_cycle(conn, cycle_id="corrupt-2", rooms_json="<<<not json>>>")
        # The room isn't in rooms_json at all (it's unparseable), so the name
        # must come from the live rooms table.
        resp = await client.post(
            "/api/rooms", json={"name": "Attic", "thermostat_entity_id": THERMO}
        )
        room_id = (await resp.json())["id"]
        await db.upsert_room_cycle_state(
            conn,
            RoomCycleState(
                cycle_id="corrupt-2",
                room_id=room_id,
                target_temp=70.0,
                trigger_detail="{definitely: not json",
            ),
        )
        await conn.commit()

        resp = await client.get("/api/logs/corrupt-2/detail")
        assert resp.status == 200
        body = await resp.json()
        assert len(body["rooms"]) == 1
        room = body["rooms"][0]
        assert room["trigger_detail"] is None
        # rooms_json was unparseable → no meta, so source is None and the name
        # is recovered from the live room record.
        assert room["source"] is None
        assert room["name"] == "Attic"


# ---------------------------------------------------------------------------
# Outside-temperature entity setting (2578-2579, 2603)
# ---------------------------------------------------------------------------


class TestOutsideTempEntityEdges:
    @pytest.mark.asyncio
    async def test_get_reports_null_value_when_ha_read_raises(self, client, fake_ha):
        conn = await _conn(client)
        await db.set_system_setting(conn, "outside_temperature_entity_id", "sensor.flaky")

        def _boom(entity_id, max_age_min=None):
            raise RuntimeError("ha exploded: secret internals")

        fake_ha.get_numeric_state = _boom

        resp = await client.get("/api/settings/outside-temp-entity")
        assert resp.status == 200
        body = await resp.json()
        # The entity id is still reported; only the reading degrades to null.
        assert body == {"entity_id": "sensor.flaky", "current_value": None}
        assert "secret internals" not in await resp.text()

    @pytest.mark.asyncio
    async def test_put_rejects_non_string_entity_id(self, client):
        resp = await client.put("/api/settings/outside-temp-entity", json={"entity_id": 42})
        assert resp.status == 400
        assert (await resp.json())["error"] == "entity_id must be a string or null"
        # Nothing was stored.
        got = await (await client.get("/api/settings/outside-temp-entity")).json()
        assert got["entity_id"] is None


# ---------------------------------------------------------------------------
# Sensor staleness settings + health (2628-2629, 2642, 2669-2670, 2682)
# ---------------------------------------------------------------------------


class TestSensorStalenessEdges:
    @pytest.mark.asyncio
    async def test_get_falls_back_to_engine_default_on_corrupt_setting(self, client):
        from backend.engine.cycle_engine import SENSOR_STALE_AFTER_MIN

        conn = await _conn(client)
        await db.set_system_setting(conn, "sensor_stale_after_min", "not-a-number")

        resp = await client.get("/api/settings/sensor-staleness")
        assert resp.status == 200
        assert (await resp.json())["stale_after_min"] == float(SENSOR_STALE_AFTER_MIN)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["45", None, {"x": 1}, [45]])
    async def test_put_rejects_non_numeric_threshold(self, client, bad):
        resp = await client.put("/api/settings/sensor-staleness", json={"stale_after_min": bad})
        assert resp.status == 400
        assert (await resp.json())["error"] == "stale_after_min must be a number (minutes)"

    @pytest.mark.asyncio
    async def test_put_rejecting_the_value_leaves_the_stored_one(self, client):
        await client.put("/api/settings/sensor-staleness", json={"stale_after_min": 45})
        resp = await client.put("/api/settings/sensor-staleness", json={"stale_after_min": "45"})
        assert resp.status == 400
        got = await (await client.get("/api/settings/sensor-staleness")).json()
        assert got["stale_after_min"] == 45.0

    @pytest.mark.asyncio
    async def test_health_uses_engine_default_when_setting_is_corrupt(self, client, fake_ha):
        from backend.engine.cycle_engine import SENSOR_STALE_AFTER_MIN

        conn = await _conn(client)
        await db.set_system_setting(conn, "sensor_stale_after_min", "")

        resp = await client.post(
            "/api/rooms", json={"name": "Cellar", "thermostat_entity_id": THERMO}
        )
        room_id = (await resp.json())["id"]
        await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.cellar"})
        # Older than the engine default → reported stale under the fallback.
        fake_ha.seed_state(
            "sensor.cellar",
            "70",
            {"unit_of_measurement": "°F"},
            last_updated=(
                datetime.now(UTC) - timedelta(minutes=SENSOR_STALE_AFTER_MIN + 30)
            ).isoformat(),
        )

        body = await (await client.get("/api/sensor-health")).json()
        assert body["stale_after_min"] == float(SENSOR_STALE_AFTER_MIN)
        assert [r["room_id"] for r in body["rooms"]] == [room_id]
        sensor = body["rooms"][0]["stale_sensors"][0]
        assert sensor["reason"] == "stale"
        assert sensor["age_seconds"] > SENSOR_STALE_AFTER_MIN * 60

    @pytest.mark.asyncio
    async def test_health_reports_sensor_never_seen_in_cache(self, client, fake_ha):
        resp = await client.post(
            "/api/rooms", json={"name": "Loft", "thermostat_entity_id": THERMO}
        )
        room_id = (await resp.json())["id"]
        await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.ghost"})
        await client.post(f"/api/rooms/{room_id}/sensors", json={"entity_id": "sensor.present"})
        fake_ha.seed_state("sensor.present", "70", {"unit_of_measurement": "°F"})

        body = await (await client.get("/api/sensor-health")).json()
        assert [r["room_id"] for r in body["rooms"]] == [room_id]
        stale = body["rooms"][0]["stale_sensors"]
        # Only the never-seen sensor is reported, with a null age so the UI can
        # tell "never seen" apart from "went stale".
        assert stale == [
            {"entity_id": "sensor.ghost", "age_seconds": None, "reason": "not_in_cache"}
        ]


# ---------------------------------------------------------------------------
# Vacation mode: naive ISO timestamps are treated as UTC (2887)
# ---------------------------------------------------------------------------


class TestVacationModeNaiveTimestamp:
    @pytest.mark.asyncio
    async def test_naive_return_at_is_interpreted_as_utc(self, client):
        naive = (datetime.now(UTC) + timedelta(days=3)).replace(tzinfo=None, microsecond=0)
        resp = await client.post(
            "/api/settings/vacation-mode", json={"return_at": naive.isoformat()}
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["enabled"] is True
        # Echoed back with an explicit UTC offset — the naive value was stamped
        # UTC, not shifted into local time.
        assert body["return_at"] == naive.replace(tzinfo=UTC).isoformat()

        got = await (await client.get("/api/settings/vacation-mode")).json()
        assert got["enabled"] is True
        assert datetime.fromisoformat(got["return_at"]).replace(tzinfo=UTC) == naive.replace(
            tzinfo=UTC
        )

    @pytest.mark.asyncio
    async def test_naive_return_at_in_the_past_is_still_rejected(self, client):
        naive = (datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None)
        resp = await client.post(
            "/api/settings/vacation-mode", json={"return_at": naive.isoformat()}
        )
        assert resp.status == 400
        assert "future" in (await resp.json())["error"]


# ---------------------------------------------------------------------------
# Vacation test-mode command + revert (2986-2988, 3008-3015)
# ---------------------------------------------------------------------------


class TestVacationTestCommands:
    @pytest.mark.asyncio
    async def test_range_command_failure_returns_502_without_leaking_detail(self, client, fake_ha):
        await _register_thermostat(client)

        async def _boom(entity_id, low, high):
            raise RuntimeError("ha socket closed: /secret/path")

        fake_ha.set_thermostat_temperature_range = _boom

        resp = await client.post(f"/api/thermostats/{THERMO}/test-vacation")
        assert resp.status == 502
        text = await resp.text()
        assert (await resp.json())["error"] == "Failed to send range command"
        assert "secret" not in text
        assert "RuntimeError" not in text

    @pytest.mark.asyncio
    async def test_revert_sends_hvac_mode_off(self, client, fake_ha):
        await _register_thermostat(client)
        fake_ha.seed_state(THERMO, "heat_cool", {"current_temperature": 70.0})

        resp = await client.delete(f"/api/thermostats/{THERMO}/test-vacation")
        assert resp.status == 200
        assert await resp.json() == {"ok": True}

        mode_calls = [
            c for c in fake_ha.calls if c.domain == "climate" and c.service == "set_hvac_mode"
        ]
        assert [c.data["hvac_mode"] for c in mode_calls] == ["off"]
        assert mode_calls[0].data["entity_id"] == THERMO
        # And the fake's own state actually flipped.
        assert fake_ha.get_state(THERMO)["state"] == "off"

    @pytest.mark.asyncio
    async def test_revert_failure_returns_502_without_leaking_detail(self, client, fake_ha):
        await _register_thermostat(client)

        async def _boom(entity_id, mode):
            raise RuntimeError("ha socket closed: /secret/path")

        fake_ha.set_thermostat_hvac_mode = _boom

        resp = await client.delete(f"/api/thermostats/{THERMO}/test-vacation")
        assert resp.status == 502
        text = await resp.text()
        assert (await resp.json())["error"] == "Failed to revert thermostat mode"
        assert "secret" not in text


# ---------------------------------------------------------------------------
# Rollup triggers with an unparseable body (3049-3050, 3065-3066)
# ---------------------------------------------------------------------------


class TestRollupTriggerBadBody:
    @pytest.mark.asyncio
    async def test_daily_rollup_ignores_unparseable_body(self, client):
        resp = await client.post(
            "/api/metrics/rollup/daily",
            data="{ this is not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 200
        # Body discarded → the documented default window applies.
        assert await resp.json() == {"rows_written": 0, "days_back": 1}

    @pytest.mark.asyncio
    async def test_monthly_rollup_ignores_unparseable_body(self, client):
        resp = await client.post(
            "/api/metrics/rollup/monthly",
            data="{ this is not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 200
        assert await resp.json() == {"rows_written": 0, "months_back": 1}


# ---------------------------------------------------------------------------
# Retention floor + date-range parsing fallbacks (3081-3082, 3135-3140)
# ---------------------------------------------------------------------------


class TestDateRangeFallbacks:
    @pytest.mark.asyncio
    async def test_corrupt_retention_setting_falls_back_to_thirty_days(self, client):
        conn = await _conn(client)
        await db.set_system_setting(conn, "cycle_log_retention_days", "twelve")
        await _insert_raw_cycle(
            conn, cycle_id="in-window", rooms_json="{}", started_at=_noon_utc(10)
        )
        await _insert_raw_cycle(conn, cycle_id="purged", rooms_json="{}", started_at=_noon_utc(35))

        # Ask for 60 days back; the floor clamps to the 30-day default, so the
        # 35-day-old cycle stays out and the 10-day-old one comes through.
        resp = await client.get(f"/api/logs?start={_local_date(60)}")
        assert resp.status == 200
        assert {c["id"] for c in await resp.json()} == {"in-window"}

    @pytest.mark.asyncio
    async def test_non_integer_days_param_falls_back_to_default_window(self, client):
        resp = await client.get(f"/api/metrics/thermostats/{THERMO}/summary?days=lots")
        assert resp.status == 200
        body = await resp.json()
        assert body["start_date"] == _local_date(6)
        assert body["end_date"] == _local_date(0)

    @pytest.mark.asyncio
    async def test_unparseable_end_anchors_the_window_on_today(self, client):
        """An `end` that is not an ISO date cannot anchor the window, so the
        start is measured back from *today* instead. Driven through the CSV
        export because it is the range consumer that treats `end` as an opaque
        string, so the only observable effect is which rows survive the window.
        """
        conn = await _conn(client)
        await _insert_raw_cycle(conn, cycle_id="near", rooms_json="{}", started_at=_noon_utc(3))
        await _insert_raw_cycle(conn, cycle_id="far", rooms_json="{}", started_at=_noon_utc(10))

        resp = await client.get("/api/metrics/export.csv?days=7&end=totally-not-a-date")
        assert resp.status == 200
        rows = list(csv.reader(io.StringIO(await resp.text())))
        ids = {r[0] for r in rows[1:]}
        # Window is [today-6, "totally-not-a-date"]: the 3-day-old cycle is in,
        # the 10-day-old one is before the today-anchored start.
        assert ids == {"near"}


# ---------------------------------------------------------------------------
# Live snapshot: outside-temp read failure (3418-3419)
# ---------------------------------------------------------------------------


class TestLiveOutsideTempFailure:
    @pytest.mark.asyncio
    async def test_live_degrades_to_null_when_outside_read_raises(self, client, fake_ha):
        conn = await _conn(client)
        await db.set_system_setting(conn, "outside_temperature_entity_id", "sensor.flaky_out")

        def _boom(entity_id, max_age_min=None):
            raise RuntimeError("cache miss: /secret/path")

        fake_ha.get_numeric_state = _boom

        resp = await client.get(f"/api/metrics/thermostats/{THERMO}/live")
        assert resp.status == 200
        body = await resp.json()
        assert body["outside_temp_entity_id"] == "sensor.flaky_out"
        assert body["current_outside_temp"] is None
        assert "secret" not in await resp.text()


# ---------------------------------------------------------------------------
# CSV export: unparseable timestamps yield a blank duration (3506-3507)
# ---------------------------------------------------------------------------


class TestCsvExportBadTimestamps:
    @pytest.mark.asyncio
    async def test_unparseable_ended_at_leaves_duration_blank(self, client):
        conn = await _conn(client)
        started = _noon_utc(0)
        await _insert_raw_cycle(
            conn,
            cycle_id="bad-ts",
            rooms_json="{}",
            started_at=started,
            ended_at="whenever",
        )
        await _insert_raw_cycle(
            conn,
            cycle_id="good-ts",
            rooms_json="{}",
            started_at=started,
            ended_at=(started + timedelta(minutes=15)).replace(tzinfo=None).isoformat(),
        )

        resp = await client.get(
            f"/api/metrics/export.csv?start={_local_date(0)}&end={_local_date(0)}"
        )
        assert resp.status == 200
        rows = list(csv.reader(io.StringIO(await resp.text())))
        header = rows[0]
        dur_idx = header.index("duration_seconds")
        by_id = {r[0]: r for r in rows[1:]}
        assert by_id["bad-ts"][dur_idx] == ""
        assert by_id["good-ts"][dur_idx] == "900"
        # The row is still exported in full rather than dropped.
        assert by_id["bad-ts"][header.index("ended_at")] == "whenever"


# ---------------------------------------------------------------------------
# Demo seeder: unparseable body + seeder failure (3697-3698, 3713-3715)
# ---------------------------------------------------------------------------


class TestSeedDemoMetricsEdges:
    @pytest.mark.asyncio
    async def test_unparseable_body_uses_the_documented_defaults(self, client):
        await _register_thermostat(client)
        resp = await client.post(
            "/api/rooms", json={"name": "Parlour", "thermostat_entity_id": THERMO}
        )
        assert resp.status == 201, await resp.text()
        assert (await client.post("/api/system/dev-mode", json={"dev_mode": True})).status == 200

        resp = await client.post(
            "/api/dev/seed-demo-metrics",
            data="{oops",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 200
        body = await resp.json()
        expected_end = (
            date.fromisoformat(demo_seed.DEFAULT_START_DATE)
            + timedelta(days=demo_seed.DEFAULT_DAYS - 1)
        ).isoformat()
        assert body["start_date"] == demo_seed.DEFAULT_START_DATE
        assert body["end_date"] == expected_end
        assert body["seeded_cycles"] > 0

        # The rows really landed in the default window.
        logs = await (
            await client.get(
                f"/api/logs?start={demo_seed.DEFAULT_START_DATE}&end={expected_end}&limit=5"
            )
        ).json()
        assert logs
        assert all(c["id"].startswith("demo-") for c in logs)

    @pytest.mark.asyncio
    async def test_seeder_failure_returns_500_without_leaking_detail(self, client, monkeypatch):
        await _register_thermostat(client)
        assert (await client.post("/api/system/dev-mode", json={"dev_mode": True})).status == 200

        async def _boom(conn, start_date, days):
            raise RuntimeError("seed blew up on /secret/path")

        monkeypatch.setattr(demo_seed, "seed_demo_metrics", _boom)

        resp = await client.post("/api/dev/seed-demo-metrics", json={})
        assert resp.status == 500
        text = await resp.text()
        assert (await resp.json())["error"] == "Failed to seed demo metrics"
        assert "secret" not in text
        assert "RuntimeError" not in text


# ---------------------------------------------------------------------------
# Backup / restore failure paths (3729, 3754-3756, 3804-3808)
# ---------------------------------------------------------------------------


class TestBackupRestoreFailures:
    @pytest.mark.asyncio
    async def test_backup_404s_when_the_db_file_is_gone(self, client, monkeypatch, tmp_path):
        import backend.api.routes as routes_mod

        missing = str(tmp_path / "vanished.db")
        real_exists = routes_mod.os.path.exists
        db_path = client.app["db_path"]

        def _exists(path):
            # Only lie about the app's DB path; everything else is untouched.
            return False if path == db_path else real_exists(path)

        monkeypatch.setattr(routes_mod.os.path, "exists", _exists)
        assert not real_exists(missing)

        resp = await client.get("/api/backup")
        assert resp.status == 404
        assert (await resp.json())["error"] == "Database file not found"

    @pytest.mark.asyncio
    async def test_backup_snapshot_failure_returns_500_without_leaking_detail(
        self, client, monkeypatch
    ):
        db_path = client.app["db_path"]
        real_connect = _sqlite3.connect

        def _connect(target, *args, **kwargs):
            if target == db_path:
                raise _sqlite3.OperationalError("disk I/O error on /secret/path")
            return real_connect(target, *args, **kwargs)

        monkeypatch.setattr(_sqlite3, "connect", _connect)

        resp = await client.get("/api/backup")
        assert resp.status == 500
        text = await resp.text()
        assert (await resp.json())["error"] == "Backup failed"
        assert "secret" not in text
        assert "OperationalError" not in text

    @pytest.mark.asyncio
    async def test_restore_failure_returns_500_and_leaves_the_db_intact(
        self, client, monkeypatch, db_path
    ):
        import backend.api.routes as routes_mod

        # Take a real backup so the upload passes the SQLite magic-byte check
        # and the handler reaches the swap.
        db_bytes = await (await client.get("/api/backup")).read()
        assert db_bytes[:16] == b"SQLite format 3\x00"

        moved: list = []

        def _boom(src, dst):
            moved.append((src, dst))
            raise OSError("cross-device link on /secret/path")

        monkeypatch.setattr(routes_mod.shutil, "move", _boom)

        resp = await client.post("/api/restore", data={"file": io.BytesIO(db_bytes)})
        assert resp.status == 500
        text = await resp.text()
        assert (await resp.json())["error"] == "Restore failed"
        assert "secret" not in text
        assert "OSError" not in text

        # The move was attempted, the uploaded temp file was cleaned up, and
        # the live DB still answers queries.
        assert len(moved) == 1
        assert not routes_mod.os.path.exists(moved[0][0])
        assert (await client.get("/api/rooms")).status == 200


# ---------------------------------------------------------------------------
# Sanity: the corrupt-JSON list path still reports overflow/eco flags, so the
# fallback in _cycle_log_to_dict doesn't swallow the rest of the payload.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corrupt_rooms_json_still_reports_overflow_and_eco_flags(client):
    conn = await _conn(client)
    await _insert_raw_cycle(conn, cycle_id="flags-1", rooms_json="nope")
    resp = await client.post("/api/rooms", json={"name": "Den", "thermostat_entity_id": THERMO})
    room_id = (await resp.json())["id"]
    await db.upsert_room_cycle_state(
        conn,
        RoomCycleState(
            cycle_id="flags-1",
            room_id=room_id,
            target_temp=70.0,
            role="overflow",
            eco_active=True,
        ),
    )
    await conn.commit()

    body = await (await client.get("/api/logs")).json()
    entry = next(c for c in body if c["id"] == "flags-1")
    assert entry["rooms"] == {}
    assert entry["had_overflow"] is True
    assert entry["eco_active"] is True
