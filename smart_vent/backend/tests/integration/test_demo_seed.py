"""
Integration tests for the demo metrics seeder (Issue #442).

Covers the dev-mode gate, input validation, determinism/idempotence of
``POST /api/dev/seed-demo-metrics``, the new per-day series on the
eco-impact endpoints, and the new ``short_cycles`` timeseries metric —
all driven through the public HTTP surface.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from backend import db
from backend.models import CycleLog

THERMO_A = "climate.demo_a"
THERMO_B = "climate.demo_b"

START = "2025-06-01"
END = "2025-06-07"
RANGE = f"start={START}&end={END}"


async def _register_home(client) -> None:
    """Two thermostats with two rooms each; one room carries a vent so the
    seeder's vent-event path is exercised."""
    for entity_id, name in ((THERMO_A, "Demo A"), (THERMO_B, "Demo B")):
        resp = await client.post(
            "/api/thermostats",
            json={"thermostat_entity_id": entity_id, "name": name, "total_vents_count": 4},
        )
        assert resp.status == 201, await resp.text()
    for room_name, thermo, vent in (
        ("Alpha", THERMO_A, "cover.alpha_vent"),
        ("Beta", THERMO_A, None),
        ("Gamma", THERMO_B, "cover.gamma_vent"),
        ("Delta", THERMO_B, None),
    ):
        resp = await client.post(
            "/api/rooms", json={"name": room_name, "thermostat_entity_id": thermo}
        )
        assert resp.status == 201, await resp.text()
        room_id = (await resp.json())["id"]
        if vent:
            resp = await client.post(f"/api/rooms/{room_id}/vents", json={"entity_id": vent})
            assert resp.status == 201, await resp.text()


async def _enable_dev_mode(client) -> None:
    resp = await client.post("/api/system/dev-mode", json={"dev_mode": True})
    assert resp.status == 200


async def _seed(client, body: dict | None = None):
    return await client.post("/api/dev/seed-demo-metrics", json=body or {})


@pytest.mark.asyncio
async def test_seed_requires_dev_mode(client) -> None:
    resp = await _seed(client)
    assert resp.status == 403
    body = await resp.json()
    assert "Developer mode" in body["error"]


@pytest.mark.asyncio
async def test_seed_validates_inputs(client) -> None:
    await _enable_dev_mode(client)
    resp = await _seed(client, {"start_date": "junk"})
    assert resp.status == 400
    resp = await _seed(client, {"days": 0})
    assert resp.status == 400
    resp = await _seed(client, {"days": 32})
    assert resp.status == 400
    resp = await _seed(client, {"days": "many"})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_seed_is_deterministic_and_idempotent(client) -> None:
    """Reseeding must replace the demo rows with byte-identical data — the
    E2E golden screenshots depend on run-to-run pixel equality."""
    await _register_home(client)
    await _enable_dev_mode(client)

    first = await (await _seed(client)).json()
    assert first["seeded_cycles"] > 0
    assert first["eco_cycles"] > 0
    assert first["thermostats"] == 2
    assert first["start_date"] == START
    assert first["end_date"] == END

    impact_1 = await (await client.get(f"/api/metrics/thermostats/eco-impact?{RANGE}")).json()

    second = await (await _seed(client)).json()
    assert second == first

    conn = await client.app["scheduler"].get_db()
    async with conn.execute("SELECT COUNT(*) AS n FROM cycle_logs") as cur:
        n_cycles = (await cur.fetchone())["n"]
    assert n_cycles == first["seeded_cycles"]  # no duplicates piled up

    impact_2 = await (await client.get(f"/api/metrics/thermostats/eco-impact?{RANGE}")).json()
    assert impact_2 == impact_1


@pytest.mark.asyncio
async def test_seed_leaves_real_cycles_alone(client) -> None:
    """Only ``demo-`` prefixed rows are wiped on reseed."""
    await _register_home(client)
    await _enable_dev_mode(client)

    conn = await client.app["scheduler"].get_db()
    real_start = datetime(2025, 6, 3, 12, 0, 0)
    real = CycleLog(
        id="real-cycle-1",
        thermostat_entity_id=THERMO_A,
        started_at=real_start,
        mode="cooling",
        rooms_json=json.dumps({}),
    )
    await db.insert_cycle_log(conn, real)
    await db.close_cycle_log(
        conn, "real-cycle-1", ended_at=real_start + timedelta(minutes=20), ended_reason="completed"
    )

    await _seed(client)
    await _seed(client)

    async with conn.execute("SELECT COUNT(*) AS n FROM cycle_logs WHERE id='real-cycle-1'") as cur:
        assert (await cur.fetchone())["n"] == 1


@pytest.mark.asyncio
async def test_purge_exempts_demo_rows_but_still_purges_real_ones(client) -> None:
    """Retention would otherwise delete the fixed past demo window on its next
    pass; real rows past retention must still be purged as before."""
    await _register_home(client)
    await _enable_dev_mode(client)
    await _seed(client)

    conn = await client.app["scheduler"].get_db()
    old_start = datetime(2025, 6, 3, 12, 0, 0)
    stale = CycleLog(
        id="real-old-cycle",
        thermostat_entity_id=THERMO_A,
        started_at=old_start,
        mode="cooling",
        rooms_json=json.dumps({}),
    )
    await db.insert_cycle_log(conn, stale)
    await db.close_cycle_log(
        conn, "real-old-cycle", ended_at=old_start + timedelta(minutes=20), ended_reason="completed"
    )

    deleted = await db.purge_cycle_logs(conn, older_than_days=30)
    assert deleted == 1  # the stale real row, nothing else

    async with conn.execute("SELECT COUNT(*) AS n FROM cycle_logs WHERE id LIKE 'demo-%'") as cur:
        assert (await cur.fetchone())["n"] > 0
    async with conn.execute(
        "SELECT COUNT(*) AS n FROM cycle_logs WHERE id='real-old-cycle'"
    ) as cur:
        assert (await cur.fetchone())["n"] == 0

    # The retention clamp opens up to the surviving demo rows: an explicit
    # query for the demo window still returns data (Issue #442).
    impact = await (await client.get(f"/api/metrics/thermostats/eco-impact?{RANGE}")).json()
    assert impact["start_date"] == START
    assert impact["total_cycles"] > 0


@pytest.mark.asyncio
async def test_eco_impact_day_series(client) -> None:
    """The per-day series must reconcile with the range-wide totals and carry
    eco engagement only on the hot (late-week) seeded days."""
    await _register_home(client)
    await _enable_dev_mode(client)
    await _seed(client)

    impact = await (await client.get(f"/api/metrics/thermostats/eco-impact?{RANGE}")).json()
    days = impact["days"]
    assert [d["date"] for d in days] == sorted(d["date"] for d in days)
    assert sum(d["total_cycles"] for d in days) == impact["total_cycles"]
    assert sum(d["eco_active_cycles"] for d in days) == impact["eco_active_cycles"]
    assert sum(d["total_seconds"] for d in days) == impact["total_seconds"]
    assert sum(d["eco_active_seconds"] for d in days) == impact["eco_active_seconds"]

    # Seeded outdoor temps only cross the 86°F eco threshold late in the week,
    # so early days must show zero engagement and some later day non-zero.
    assert days[0]["eco_active_cycles"] == 0
    assert days[0]["avg_drift_f"] == 0.0
    eco_days = [d for d in days if d["eco_active_cycles"] > 0]
    assert eco_days
    assert all(d["avg_drift_f"] > 0 for d in eco_days)

    # Per-thermostat variant carries the same day keys.
    per_t = await (
        await client.get(f"/api/metrics/thermostats/{THERMO_A}/eco-impact?{RANGE}")
    ).json()
    assert {d["date"] for d in per_t["days"]} <= {d["date"] for d in days}
    assert per_t["days"][0]["total_cycles"] > 0

    # Room breakdown order must be deterministic across installs: cycle count
    # descending, ties broken by name. Random-UUID tie order flipped the
    # "Eco drift by room" chart between fresh E2E stacks (golden churn).
    rooms = impact["rooms"]
    key = [(-r["eco_active_cycles"], r["name"]) for r in rooms]
    assert key == sorted(key)
    assert any(
        rooms[i]["eco_active_cycles"] == rooms[i + 1]["eco_active_cycles"]
        for i in range(len(rooms) - 1)
    ), "fixture should contain a tie so the ordering guarantee is actually exercised"


@pytest.mark.asyncio
async def test_short_cycles_timeseries(client) -> None:
    """`short_cycles` counts sub-10-minute cycles per day. The seeder plants
    one 6-minute cycle on every even-indexed day for each thermostat."""
    await _register_home(client)
    await _enable_dev_mode(client)
    await _seed(client)

    resp = await client.get(
        f"/api/metrics/thermostats/{THERMO_A}/timeseries?metric=short_cycles&{RANGE}"
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["metric"] == "short_cycles"
    by_day = {p["period"]: p["value"] for p in data["series"]}
    # Days 0/2/4/6 of the seeded week end with a 6-minute cycle.
    assert by_day["2025-06-01"] == 1
    assert by_day["2025-06-02"] == 0
    assert by_day["2025-06-03"] == 1

    # Unknown metric is still rejected.
    resp = await client.get(f"/api/metrics/thermostats/{THERMO_A}/timeseries?metric=bogus")
    assert resp.status == 400


@pytest.mark.asyncio
async def test_seeded_charts_have_data_everywhere(client) -> None:
    """Every Metrics-page data feed renders non-empty from the seed — this is
    the property the E2E golden screenshots rely on."""
    await _register_home(client)
    await _enable_dev_mode(client)
    await _seed(client)

    summary = await (
        await client.get(f"/api/metrics/thermostats/{THERMO_A}/summary?{RANGE}")
    ).json()
    assert summary["cycle_count"] > 0
    assert summary["heating_seconds"] > 0
    assert summary["cooling_seconds"] > 0
    assert summary["completed_count"] > 0
    assert summary["timeout_count"] > 0
    assert summary["aborted_count"] > 0
    assert summary["eco_cycle_count"] > 0
    assert summary["avg_outside_temp_at_start"] is not None
    assert summary["source_breakdown"]["schedule"] > 0
    assert summary["source_breakdown"]["presence"] > 0
    assert summary["source_breakdown"]["override"] > 0

    for metric in ("hours", "cycles", "avg_duration", "duty_cycle", "time_to_target"):
        data = await (
            await client.get(
                f"/api/metrics/thermostats/{THERMO_A}/timeseries?metric={metric}&{RANGE}"
            )
        ).json()
        assert data["series"], f"metric {metric} came back empty"

    scatter = await (
        await client.get(f"/api/metrics/thermostats/{THERMO_A}/cycles-vs-outside-temp?{RANGE}")
    ).json()
    assert scatter["points"]
    assert any(p["eco_active"] for p in scatter["points"])
    assert any(not p["eco_active"] for p in scatter["points"])

    rooms = await (await client.get(f"/api/metrics/thermostats/{THERMO_A}/rooms?{RANGE}")).json()
    assert rooms["rooms"] and any(r["participation_count"] > 0 for r in rooms["rooms"])

    histogram = await (
        await client.get(f"/api/metrics/thermostats/{THERMO_A}/overshoot-histogram?{RANGE}")
    ).json()
    assert histogram["total_room_cycles"] > 0
    assert histogram["overshot_count"] > 0

    heatmap = await (
        await client.get(f"/api/metrics/thermostats/{THERMO_A}/hour-heatmap?{RANGE}")
    ).json()
    assert any(any(row) for row in heatmap["grid_seconds"])

    timeline = await (
        await client.get(f"/api/metrics/thermostats/{THERMO_A}/vent-timeline?{RANGE}")
    ).json()
    assert timeline["events"]
    actions = {e["action"] for e in timeline["events"]}
    assert "opened_at_start" in actions
    assert "closed_reached_target" in actions
