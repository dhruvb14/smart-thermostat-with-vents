"""
Integration tests for Issue #85 Phase 2 — backend metrics read API.

Covers all nine endpoints (2a–2i) by seeding completed cycle_logs
directly into the app's DB and then driving the public HTTP surface
through ``client``. The ``fake_ha`` is only needed for the live
endpoint's outside-temperature reading.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from backend import db
from backend.models import CycleLog, Room, RoomCycleState

THERMO_A = "climate.thermo_a"
THERMO_B = "climate.thermo_b"


async def _seed_cycle(
    conn,
    *,
    cycle_id: str,
    thermostat: str = THERMO_A,
    started_at: datetime,
    duration: timedelta,
    mode: str = "cooling",
    ended_reason: str = "completed",
    outside_temp_at_start: float | None = None,
    outside_temp_at_end: float | None = None,
    rooms_json: dict | None = None,
) -> None:
    log_ = CycleLog(
        id=cycle_id,
        thermostat_entity_id=thermostat,
        started_at=started_at,
        mode=mode,
        rooms_json=json.dumps(rooms_json or {}),
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


async def _conn(client):
    return await client.app["scheduler"].get_db()


@pytest.fixture
def today_iso() -> str:
    return datetime.now().date().isoformat()  # noqa: DTZ005


@pytest.fixture
def today_dt() -> datetime:
    """Today at 12:00 UTC — far enough from local midnight to avoid the
    local-bucketing flipping the date for any reasonable timezone."""
    return datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# 2a: per-thermostat summary
# ---------------------------------------------------------------------------


class TestSummary:
    @pytest.mark.asyncio
    async def test_per_thermostat_summary(self, client, today_iso, today_dt):
        conn = await _conn(client)
        await _seed_cycle(
            conn,
            cycle_id="a1",
            started_at=today_dt,
            duration=timedelta(minutes=30),
            mode="cooling",
            outside_temp_at_start=80.0,
            outside_temp_at_end=78.0,
            rooms_json={"r1": {"name": "r", "target": 72.0, "source": "schedule"}},
        )
        await _seed_cycle(
            conn,
            cycle_id="a2",
            started_at=today_dt + timedelta(hours=1),
            duration=timedelta(minutes=10),
            mode="heating",
            ended_reason="aborted: timeout",
        )

        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO_A}/summary?start={today_iso}&end={today_iso}"
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["thermostat_entity_id"] == THERMO_A
        assert body["cycle_count"] == 2
        assert body["completed_count"] == 1
        assert body["timeout_count"] == 1
        assert body["aborted_count"] == 0
        assert body["cooling_seconds"] == 30 * 60
        assert body["heating_seconds"] == 10 * 60
        assert body["avg_outside_temp_at_start"] == pytest.approx(80.0)
        # Only the cooling cycle had a 'schedule' room, so source map = {schedule:1}
        assert body["source_breakdown"]["schedule"] == 1

    @pytest.mark.asyncio
    async def test_summary_excludes_other_thermostat(self, client, today_iso, today_dt):
        conn = await _conn(client)
        await _seed_cycle(
            conn,
            cycle_id="x1",
            thermostat=THERMO_B,
            started_at=today_dt,
            duration=timedelta(minutes=20),
        )
        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO_A}/summary?start={today_iso}&end={today_iso}"
        )
        body = await resp.json()
        assert body["cycle_count"] == 0
        assert body["cooling_seconds"] == 0


# ---------------------------------------------------------------------------
# 2b: home aggregate summary
# ---------------------------------------------------------------------------


class TestHomeSummary:
    @pytest.mark.asyncio
    async def test_aggregates_across_thermostats(self, client, today_iso, today_dt):
        conn = await _conn(client)
        await _seed_cycle(
            conn,
            cycle_id="h1",
            thermostat=THERMO_A,
            started_at=today_dt,
            duration=timedelta(minutes=15),
        )
        await _seed_cycle(
            conn,
            cycle_id="h2",
            thermostat=THERMO_B,
            started_at=today_dt + timedelta(hours=2),
            duration=timedelta(minutes=25),
        )
        resp = await client.get(
            f"/api/metrics/thermostats/summary?start={today_iso}&end={today_iso}"
        )
        body = await resp.json()
        assert body["thermostat_entity_id"] is None
        assert body["cycle_count"] == 2
        assert body["thermostat_count"] == 2
        assert body["cooling_seconds"] == (15 + 25) * 60


# ---------------------------------------------------------------------------
# 2c: timeseries
# ---------------------------------------------------------------------------


class TestTimeseries:
    @pytest.mark.asyncio
    async def test_hours_per_day(self, client, today_iso, today_dt):
        conn = await _conn(client)
        await _seed_cycle(
            conn,
            cycle_id="t1",
            started_at=today_dt,
            duration=timedelta(minutes=20),
            mode="cooling",
        )
        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO_A}/timeseries"
            f"?metric=hours&granularity=day&start={today_iso}&end={today_iso}"
        )
        body = await resp.json()
        assert body["metric"] == "hours"
        assert body["granularity"] == "day"
        assert len(body["series"]) == 1
        assert body["series"][0]["cooling_seconds"] == 20 * 60
        assert body["series"][0]["heating_seconds"] == 0

    @pytest.mark.asyncio
    async def test_cycle_count_per_day(self, client, today_iso, today_dt):
        conn = await _conn(client)
        for i in range(3):
            await _seed_cycle(
                conn,
                cycle_id=f"c{i}",
                started_at=today_dt + timedelta(hours=i),
                duration=timedelta(minutes=10),
            )
        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO_A}/timeseries"
            f"?metric=cycles&granularity=day&start={today_iso}&end={today_iso}"
        )
        body = await resp.json()
        assert body["series"][0]["value"] == 3

    @pytest.mark.asyncio
    async def test_unknown_metric_rejected(self, client, today_iso):
        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO_A}/timeseries"
            f"?metric=banana&granularity=day&start={today_iso}&end={today_iso}"
        )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# 2d: per-room metrics
# ---------------------------------------------------------------------------


class TestPhase4BackendAdditions:
    """time_to_target + degree_minutes timeseries + overshoot histogram (#85 Phase 4f/4k/4l)."""

    @pytest.mark.asyncio
    async def test_time_to_target_timeseries(self, client, today_iso, today_dt):
        conn = await _conn(client)
        await db.upsert_room(conn, Room(id="rA", name="A", thermostat_entity_id=THERMO_A))
        await _seed_cycle(
            conn,
            cycle_id="ttt1",
            started_at=today_dt,
            duration=timedelta(minutes=30),
        )
        # Seed a room_cycle_states row with reached_at 10 min into the cycle.
        await db.upsert_room_cycle_state(
            conn,
            RoomCycleState(
                cycle_id="ttt1",
                room_id="rA",
                target_temp=72.0,
                joined_at=today_dt,
                reached_at=today_dt + timedelta(minutes=10),
                vent_closed_at=today_dt + timedelta(minutes=10),
            ),
        )
        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO_A}/timeseries"
            f"?metric=time_to_target&granularity=day&start={today_iso}&end={today_iso}"
        )
        body = await resp.json()
        assert body["metric"] == "time_to_target"
        assert len(body["series"]) == 1
        assert body["series"][0]["value"] == pytest.approx(600.0)

    @pytest.mark.asyncio
    async def test_degree_minutes_timeseries(self, client, today_iso, today_dt):
        conn = await _conn(client)
        await _seed_cycle(
            conn,
            cycle_id="dm1",
            started_at=today_dt,
            duration=timedelta(minutes=30),
        )
        # Two thermostat-level samples 10 minutes apart with |sp-temp|=2°F →
        # 2°F × 10 min = 20 degree-minutes for that interval.
        await db.insert_cycle_temp_sample(conn, "dm1", None, today_dt, None, 76.0, 74.0)
        await db.insert_cycle_temp_sample(
            conn, "dm1", None, today_dt + timedelta(minutes=10), None, 75.5, 74.0
        )
        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO_A}/timeseries"
            f"?metric=degree_minutes&granularity=day&start={today_iso}&end={today_iso}"
        )
        body = await resp.json()
        assert body["metric"] == "degree_minutes"
        assert len(body["series"]) == 1
        assert body["series"][0]["value"] == pytest.approx(20.0)

    @pytest.mark.asyncio
    async def test_overshoot_histogram(self, client, today_iso, today_dt):
        conn = await _conn(client)
        await db.upsert_room(conn, Room(id="rA", name="A", thermostat_entity_id=THERMO_A))
        await _seed_cycle(
            conn,
            cycle_id="oh1",
            started_at=today_dt,
            duration=timedelta(minutes=30),
            mode="cooling",
        )
        await db.upsert_room_cycle_state(
            conn,
            RoomCycleState(
                cycle_id="oh1",
                room_id="rA",
                target_temp=72.0,
                joined_at=today_dt,
                reached_at=today_dt + timedelta(minutes=10),
            ),
        )
        # Sample shows the cooling cycle drove temp down to 70.0 (target=72).
        # Overshoot = 2.0°F → falls in the [2, 3) bucket.
        await db.insert_cycle_temp_sample(
            conn, "oh1", "rA", today_dt + timedelta(minutes=12), 70.0, 70.0, 72.0
        )
        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO_A}/overshoot-histogram"
            f"?start={today_iso}&end={today_iso}"
        )
        body = await resp.json()
        assert body["counts"][2] == 1
        assert body["max_overshoot_f"] == pytest.approx(2.0)
        assert body["overshot_count"] == 1


class TestRoomMetrics:
    @pytest.mark.asyncio
    async def test_participation_and_durations(self, client, today_iso, today_dt):
        conn = await _conn(client)
        # Need a real Room row for the LEFT JOIN to surface.
        room = Room(id="room1", name="Bedroom", thermostat_entity_id=THERMO_A)
        await db.upsert_room(conn, room)

        await _seed_cycle(
            conn,
            cycle_id="rm1",
            started_at=today_dt,
            duration=timedelta(minutes=30),
            mode="cooling",
            rooms_json={room.id: {"name": room.name, "target": 72.0, "source": "schedule"}},
        )
        # Manually insert a room_cycle_states row so the per-room stats query
        # has something to aggregate.
        await db.upsert_room_cycle_state(
            conn,
            RoomCycleState(
                cycle_id="rm1",
                room_id=room.id,
                target_temp=72.0,
                joined_at=today_dt,
                reached_at=today_dt + timedelta(minutes=15),
                vent_closed_at=today_dt + timedelta(minutes=15),
            ),
        )

        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO_A}/rooms?start={today_iso}&end={today_iso}"
        )
        body = await resp.json()
        assert len(body["rooms"]) == 1
        r = body["rooms"][0]
        assert r["room_id"] == room.id
        assert r["participation_count"] == 1
        assert r["participation_rate"] == 1.0
        assert r["cooling_seconds"] == 15 * 60
        assert r["avg_time_to_target_seconds"] == pytest.approx(15 * 60.0)

    @pytest.mark.asyncio
    async def test_avg_time_to_target_excludes_out_of_range_cycles(
        self, client, today_iso, today_dt
    ):
        """Issue #289: a room_cycle_states row whose parent cycle falls outside
        the selected date range must not leak into avg_time_to_target_seconds.

        The date-range/thermostat filters live in the LEFT JOIN ... ON clause,
        so an out-of-range rcs row survives with cl.* = NULL. Because the row
        carries its own joined_at, COALESCE(rcs.joined_at, cl.started_at)
        resolves even when cl is NULL, so the average must be gated on the join
        actually succeeding (cl.id IS NOT NULL)."""
        conn = await _conn(client)
        room = Room(id="room1", name="Bedroom", thermostat_entity_id=THERMO_A)
        await db.upsert_room(conn, room)

        # In-range cycle today: reached target after 15 minutes.
        await _seed_cycle(
            conn,
            cycle_id="inrange",
            started_at=today_dt,
            duration=timedelta(minutes=30),
            mode="cooling",
        )
        await db.upsert_room_cycle_state(
            conn,
            RoomCycleState(
                cycle_id="inrange",
                room_id=room.id,
                target_temp=72.0,
                joined_at=today_dt,
                reached_at=today_dt + timedelta(minutes=15),
                vent_closed_at=today_dt + timedelta(minutes=15),
            ),
        )

        # Out-of-range cycle 10 days ago with its own joined_at/reached_at — must
        # be excluded entirely from the today-only window.
        old_dt = today_dt - timedelta(days=10)
        await _seed_cycle(
            conn,
            cycle_id="outofrange",
            started_at=old_dt,
            duration=timedelta(minutes=30),
            mode="cooling",
        )
        await db.upsert_room_cycle_state(
            conn,
            RoomCycleState(
                cycle_id="outofrange",
                room_id=room.id,
                target_temp=72.0,
                joined_at=old_dt,
                reached_at=old_dt + timedelta(minutes=3),
                vent_closed_at=old_dt + timedelta(minutes=3),
            ),
        )

        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO_A}/rooms?start={today_iso}&end={today_iso}"
        )
        body = await resp.json()
        assert len(body["rooms"]) == 1
        r = body["rooms"][0]
        # Only the in-range 15-minute participation counts; the 3-minute
        # out-of-range cycle must not drag the average down to 9 minutes.
        assert r["participation_count"] == 1
        assert r["avg_time_to_target_seconds"] == pytest.approx(15 * 60.0)

    @pytest.mark.asyncio
    async def test_overflow_rooms_excluded_from_room_metrics(self, client, today_iso, today_dt):
        """Issue #254: overflow room_cycle_states rows must not inflate
        per-room participation or heating/cooling time."""
        conn = await _conn(client)
        active = Room(id="active1", name="Active", thermostat_entity_id=THERMO_A)
        overflow = Room(id="overflow1", name="Overflow", thermostat_entity_id=THERMO_A)
        await db.upsert_room(conn, active)
        await db.upsert_room(conn, overflow)

        await _seed_cycle(
            conn,
            cycle_id="rmov1",
            started_at=today_dt,
            duration=timedelta(minutes=30),
            mode="cooling",
            rooms_json={active.id: {"name": active.name, "target": 72.0, "source": "schedule"}},
        )
        await db.upsert_room_cycle_state(
            conn,
            RoomCycleState(
                cycle_id="rmov1",
                room_id=active.id,
                target_temp=72.0,
                joined_at=today_dt,
                reached_at=today_dt + timedelta(minutes=15),
                vent_closed_at=today_dt + timedelta(minutes=15),
            ),
        )
        # An overflow row with full timing — must be ignored by the metric.
        await db.upsert_room_cycle_state(
            conn,
            RoomCycleState(
                cycle_id="rmov1",
                room_id=overflow.id,
                target_temp=70.0,
                joined_at=today_dt + timedelta(minutes=20),
                vent_closed_at=today_dt + timedelta(minutes=30),
                temp_at_start=75.0,
                temp_at_end=71.0,
                role="overflow",
            ),
        )

        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO_A}/rooms?start={today_iso}&end={today_iso}"
        )
        body = await resp.json()
        by_id = {r["room_id"]: r for r in body["rooms"]}
        # The overflow room still appears (it's a real room) but with zero
        # participation / conditioning time — its overflow row is not counted.
        assert by_id[overflow.id]["participation_count"] == 0
        assert by_id[overflow.id]["cooling_seconds"] == 0
        assert by_id[active.id]["participation_count"] == 1
        assert by_id[active.id]["cooling_seconds"] == 15 * 60

    @pytest.mark.asyncio
    async def test_overflow_rooms_excluded_from_overshoot(self, client, today_iso, today_dt):
        """Issue #254: an overflow room's target must not contribute to the
        overshoot histogram (it would otherwise pair with thermostat samples)."""
        conn = await _conn(client)
        await db.upsert_room(conn, Room(id="ovr", name="Ovr", thermostat_entity_id=THERMO_A))
        await _seed_cycle(
            conn,
            cycle_id="ohov1",
            started_at=today_dt,
            duration=timedelta(minutes=30),
            mode="cooling",
        )
        await db.upsert_room_cycle_state(
            conn,
            RoomCycleState(
                cycle_id="ohov1",
                room_id="ovr",
                target_temp=72.0,
                joined_at=today_dt,
                role="overflow",
            ),
        )
        # Thermostat-level sample that would register a 2°F overshoot if the
        # overflow row were joined in.
        await db.insert_cycle_temp_sample(
            conn, "ohov1", None, today_dt + timedelta(minutes=12), None, 70.0, 72.0
        )
        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO_A}/overshoot-histogram"
            f"?start={today_iso}&end={today_iso}"
        )
        body = await resp.json()
        assert body["overshot_count"] == 0
        assert all(c == 0 for c in body["counts"])


# ---------------------------------------------------------------------------
# 2e: cycles-vs-outside-temp scatter
# ---------------------------------------------------------------------------


class TestCyclesVsOutsideTemp:
    @pytest.mark.asyncio
    async def test_returns_only_cycles_with_outside_temp(self, client, today_iso, today_dt):
        conn = await _conn(client)
        await _seed_cycle(
            conn,
            cycle_id="s1",
            started_at=today_dt,
            duration=timedelta(minutes=20),
            outside_temp_at_start=85.0,
        )
        await _seed_cycle(
            conn,
            cycle_id="s2",
            started_at=today_dt + timedelta(hours=2),
            duration=timedelta(minutes=10),
            outside_temp_at_start=None,  # missing — must be excluded
        )
        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO_A}/cycles-vs-outside-temp"
            f"?start={today_iso}&end={today_iso}"
        )
        body = await resp.json()
        assert len(body["points"]) == 1
        assert body["points"][0]["outside_temp"] == pytest.approx(85.0)
        assert body["points"][0]["duration_minutes"] == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# 2f: hour heatmap
# ---------------------------------------------------------------------------


class TestHourHeatmap:
    @pytest.mark.asyncio
    async def test_grid_shape_and_totals(self, client, today_iso, today_dt):
        conn = await _conn(client)
        # One 60-minute cycle straddling 12:00–13:00 local-equivalent.
        await _seed_cycle(
            conn,
            cycle_id="hm1",
            started_at=today_dt,
            duration=timedelta(minutes=60),
        )
        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO_A}/hour-heatmap?start={today_iso}&end={today_iso}"
        )
        body = await resp.json()
        assert len(body["grid_seconds"]) == 7
        assert all(len(row) == 24 for row in body["grid_seconds"])
        assert body["day_labels"][0] == "Mon"
        # The cycle contributes exactly 3600s somewhere in the grid.
        total = sum(s for row in body["grid_seconds"] for s in row)
        assert total == 3600


# ---------------------------------------------------------------------------
# 2g: vent timeline
# ---------------------------------------------------------------------------


class TestVentTimeline:
    @pytest.mark.asyncio
    async def test_returns_events_with_disclosure(self, client, today_iso, today_dt):
        conn = await _conn(client)
        await _seed_cycle(
            conn,
            cycle_id="vt1",
            started_at=today_dt,
            duration=timedelta(minutes=20),
        )
        await db.insert_cycle_vent_event(
            conn,
            "vt1",
            today_dt,
            "cover.bedroom_vent",
            "room1",
            "opened_at_start",
            None,
        )
        await db.insert_cycle_vent_event(
            conn,
            "vt1",
            today_dt + timedelta(minutes=15),
            "cover.bedroom_vent",
            "room1",
            "closed_reached_target",
            None,
        )

        resp = await client.get(
            f"/api/metrics/thermostats/{THERMO_A}/vent-timeline?start={today_iso}&end={today_iso}"
        )
        body = await resp.json()
        assert "boundary" in body["note"].lower()
        actions = [e["action"] for e in body["events"]]
        assert actions == ["opened_at_start", "closed_reached_target"]


# ---------------------------------------------------------------------------
# 2h: live snapshot
# ---------------------------------------------------------------------------


class TestLive:
    @pytest.mark.asyncio
    async def test_live_with_no_cycles_no_outside_entity(self, client, today_iso):
        resp = await client.get(f"/api/metrics/thermostats/{THERMO_A}/live")
        body = await resp.json()
        assert body["thermostat_entity_id"] == THERMO_A
        assert body["today"]["cycle_count"] == 0
        assert body["current_cycle"] is None
        assert body["outside_temp_entity_id"] is None
        assert body["current_outside_temp"] is None

    @pytest.mark.asyncio
    async def test_live_includes_current_cycle_and_outside_temp(self, client, fake_ha, today_dt):
        # Configure outside-temp entity (°C → °F conversion: 25°C → 77°F)
        fake_ha.seed_state("sensor.outside_c", "25", {"unit_of_measurement": "°C"})
        await client.put(
            "/api/settings/outside-temp-entity",
            json={"entity_id": "sensor.outside_c"},
        )

        # Seed an OPEN cycle (ended_at NULL) so it shows up as current.
        conn = await _conn(client)
        log_ = CycleLog(
            id="open-1",
            thermostat_entity_id=THERMO_A,
            started_at=today_dt,
            mode="cooling",
            rooms_json="{}",
            thermostat_temp_at_start=78.0,
            outside_temp_at_start=77.0,
        )
        await db.insert_cycle_log(conn, log_)

        resp = await client.get(f"/api/metrics/thermostats/{THERMO_A}/live")
        body = await resp.json()
        assert body["current_cycle"] is not None
        assert body["current_cycle"]["mode"] == "cooling"
        assert body["current_cycle"]["outside_temp_at_start"] == pytest.approx(77.0)
        assert body["outside_temp_entity_id"] == "sensor.outside_c"
        assert body["current_outside_temp"] == pytest.approx(77.0)


# ---------------------------------------------------------------------------
# 2i: CSV export
# ---------------------------------------------------------------------------


class TestCsvExport:
    @pytest.mark.asyncio
    async def test_home_export_returns_all_thermostats(self, client, today_iso, today_dt):
        conn = await _conn(client)
        await _seed_cycle(
            conn,
            cycle_id="cv1",
            thermostat=THERMO_A,
            started_at=today_dt,
            duration=timedelta(minutes=10),
            outside_temp_at_start=70.0,
        )
        await _seed_cycle(
            conn,
            cycle_id="cv2",
            thermostat=THERMO_B,
            started_at=today_dt + timedelta(hours=1),
            duration=timedelta(minutes=5),
        )
        resp = await client.get(f"/api/metrics/export.csv?start={today_iso}&end={today_iso}")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/csv")
        text = await resp.text()
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        # Header + 2 data rows
        assert len(rows) == 3
        # Headers now include unit label, e.g. "outside_temp_at_start (°F)"
        assert any("outside_temp_at_start" in h for h in rows[0])
        cycle_ids = {r[0] for r in rows[1:]}
        assert cycle_ids == {"cv1", "cv2"}

    @pytest.mark.asyncio
    async def test_thermostat_scope_requires_entity_id(self, client, today_iso):
        resp = await client.get(
            f"/api/metrics/export.csv?scope=thermostat&start={today_iso}&end={today_iso}"
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_thermostat_scope_filters(self, client, today_iso, today_dt):
        conn = await _conn(client)
        await _seed_cycle(
            conn,
            cycle_id="tA",
            thermostat=THERMO_A,
            started_at=today_dt,
            duration=timedelta(minutes=10),
        )
        await _seed_cycle(
            conn,
            cycle_id="tB",
            thermostat=THERMO_B,
            started_at=today_dt + timedelta(hours=1),
            duration=timedelta(minutes=5),
        )
        resp = await client.get(
            f"/api/metrics/export.csv?scope=thermostat&entity_id={THERMO_A}"
            f"&start={today_iso}&end={today_iso}"
        )
        text = await resp.text()
        rows = list(csv.reader(io.StringIO(text)))
        cycle_ids = {r[0] for r in rows[1:]}
        assert cycle_ids == {"tA"}

    @pytest.mark.asyncio
    async def test_celsius_unit_converts_headers_and_values(self, client, today_iso, today_dt):
        """CSV export in °C mode: headers say (°C) and temperatures are converted."""
        conn = await _conn(client)
        # 32°F = 0°C — easy to verify after conversion
        await _seed_cycle(
            conn,
            cycle_id="c1",
            thermostat=THERMO_A,
            started_at=today_dt,
            duration=timedelta(minutes=10),
            outside_temp_at_start=32.0,
            outside_temp_at_end=50.0,  # 50°F = 10.0°C
        )
        # Switch the scheduler to Celsius mode for this request
        client.app["scheduler"]._active_unit = "C"
        try:
            resp = await client.get(f"/api/metrics/export.csv?start={today_iso}&end={today_iso}")
            assert resp.status == 200
            text = await resp.text()
            rows = list(csv.reader(io.StringIO(text)))
            assert len(rows) == 2  # header + 1 data row
            # Headers must include (°C) label
            assert "outside_temp_at_start (°C)" in rows[0]
            assert "outside_temp_at_end (°C)" in rows[0]
            # No (°F) label should appear
            assert not any("(°F)" in h for h in rows[0])
            # Temperature values must be converted: 32°F → 0.0°C, 50°F → 10.0°C
            outside_start_idx = rows[0].index("outside_temp_at_start (°C)")
            outside_end_idx = rows[0].index("outside_temp_at_end (°C)")
            assert rows[1][outside_start_idx] == "0.0"
            assert rows[1][outside_end_idx] == "10.0"
        finally:
            # Restore default unit so other tests are not affected
            client.app["scheduler"]._active_unit = "F"


# ---------------------------------------------------------------------------
# Default date range — endpoints work without start/end query params
# ---------------------------------------------------------------------------


class TestDefaultDateRange:
    @pytest.mark.asyncio
    async def test_summary_defaults_to_last_7_days(self, client):
        resp = await client.get(f"/api/metrics/thermostats/{THERMO_A}/summary")
        body = await resp.json()
        assert resp.status == 200
        # No cycles seeded → empty summary, but endpoint must still succeed.
        assert body["cycle_count"] == 0
        # Default range spans 7 days inclusive.
        s = datetime.fromisoformat(body["start_date"])
        e = datetime.fromisoformat(body["end_date"])
        assert (e - s).days == 6
