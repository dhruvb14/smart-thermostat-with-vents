# Metrics & analytics

The **Metrics** page (left nav → *Metrics*) shows how each thermostat — and the home as a whole — has been running. Default view is the last 7 days, switchable to any date range.

## Layout

| Section                       | What it shows                                                                                                   |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Outside-temperature picker    | Single system-wide HA entity Plenum reads at every cycle start/end. Live value displayed alongside.             |
| Filters                       | Thermostat selector ("All thermostats" by default), date-range picker, "Last 7 days" reset, **Export CSV**.     |
| Summary tiles                 | Heating time, cooling time, duty cycle %, cycle count + completed count, average outside temperature.           |
| Eco Mode impact               | Tiles (eco-relaxed cycles, eco runtime share, average drift, estimated savings) + three eco charts — shown for both the home view and a selected thermostat. See [Eco Mode](./eco-mode.md). |
| Charts (per-thermostat view)  | All 16 charts listed below.                                                                                     |
| Charts (home view)            | Completion-rate and source-breakdown donuts plus the Eco Mode impact section — the remaining per-thermostat charts only make sense for a single zone. |

## Charts

| #   | Chart                            | Endpoint                                                              |
| --- | -------------------------------- | --------------------------------------------------------------------- |
| 4a  | Heating/cooling hours per day    | `/api/metrics/thermostats/{id}/timeseries?metric=hours`               |
| 4b  | Cycles per day                   | `/api/metrics/thermostats/{id}/timeseries?metric=cycles`              |
| 4c  | Average cycle duration           | `/api/metrics/thermostats/{id}/timeseries?metric=avg_duration`        |
| 4d  | Cycles vs outside temperature    | `/api/metrics/thermostats/{id}/cycles-vs-outside-temp`                |
| 4e  | Duty cycle %                     | `/api/metrics/thermostats/{id}/timeseries?metric=duty_cycle`          |
| 4f  | Time-to-target                   | `/api/metrics/thermostats/{id}/timeseries?metric=time_to_target`      |
| 4g  | Cycle completion rate (donut)    | derived from `/api/metrics/thermostats/{id}/summary`                  |
| 4h  | Source breakdown (donut)         | derived from `/api/metrics/thermostats/{id}/summary`                  |
| 4i  | Per-room heating vs cooling time | `/api/metrics/thermostats/{id}/rooms`                                 |
| 4j  | Room participation rate          | `/api/metrics/thermostats/{id}/rooms`                                 |
| 4k  | Degree-minutes                   | `/api/metrics/thermostats/{id}/timeseries?metric=degree_minutes`      |
| 4l  | Overshoot histogram              | `/api/metrics/thermostats/{id}/overshoot-histogram`                   |
| 4m  | Hour-of-day heatmap              | `/api/metrics/thermostats/{id}/hour-heatmap`                          |
| 4n  | Vent timeline                    | `/api/metrics/thermostats/{id}/vent-timeline`                         |
| —   | Outside temperature per day      | `/api/metrics/thermostats/{id}/timeseries?metric=outside_temp`        |
| —   | Short cycles per day (<10 min)   | `/api/metrics/thermostats/{id}/timeseries?metric=short_cycles`        |
| —   | Eco-relaxed vs standard cycles   | `/api/metrics/thermostats/{id}/eco-impact` (`days` series)            |
| —   | Average Eco drift applied        | `/api/metrics/thermostats/{id}/eco-impact` (`days` series)            |
| —   | Eco drift by room                | `/api/metrics/thermostats/{id}/eco-impact` (`rooms` breakdown)        |

The eco-impact charts (and their summary tiles) also exist home-wide via `/api/metrics/thermostats/eco-impact`. The cycles-vs-outside-temp scatter colors Eco-relaxed cycles as a third (green) series. The "estimated savings" tile is a clearly-labeled rule-of-thumb (3–5% per °F of relaxation, applied to Eco-relaxed runtime) — Plenum cannot read kWh, so savings are inferred from drift, never measured.

## Outside-temperature source

Pick any HA entity in the `sensor.*` or `weather.*` domain whose state is numeric. At each cycle start and end the value is read via `HAClient.get_numeric_state()` (which normalises °C → °F) and stored on the `cycle_logs` row. Cycles still log without it; the columns just stay `NULL`.

Backfill is **not** attempted — data starts accumulating from the moment the entity is configured.

## CSV export

`Export CSV` downloads completed cycles in the active range as `metrics_<start>_<end>.csv`. Columns: `cycle_id`, `thermostat_entity_id`, `mode`, `started_at`, `ended_at`, `duration_seconds`, `ended_reason`, plus the start/end thermostat temp, setpoint, and outside-temperature columns. Scope is the home aggregate by default; selecting a thermostat in the picker scopes the export to that thermostat.

## Live snapshot endpoint (Home Assistant integration)

`GET /api/metrics/thermostats/{id}/live` returns today's running totals, the currently-running cycle (if any), and the current outside temperature. Intended for HA template sensors that want to render Plenum metrics natively.

```bash
curl http://localhost:8099/api/metrics/thermostats/climate.upstairs/live
```

## Background rollups

A daily APScheduler job rolls completed cycles into `daily_thermostat_metrics` at 00:05 local; a monthly job rolls them into `monthly_thermostat_metrics` at 00:10 on the 1st. The Metrics page's charts always query `cycle_logs` directly, even for older ranges — no chart currently reads from the rollup tables, so once a cycle ages past `cycle_log_retention_days` (default 30, cascade-deletes its samples) charts for that range go empty rather than falling back to the surviving rollup row. The rollup tables and their `db.py` readers (`get_daily_thermostat_metrics` / `get_monthly_thermostat_metrics`) exist and are exercised by tests, but nothing in `routes.py` calls them yet — treat "backs longer-horizon trends" as the intent, not the current behavior.

Manual triggers (useful during testing or after a restore):

- `POST /api/metrics/rollup/daily   {"days_back": 7}`
- `POST /api/metrics/rollup/monthly {"months_back": 3}`

## Demo data (developer mode)

`POST /api/dev/seed-demo-metrics` (or the **Seed demo metrics** button on the DevMode page) writes a deterministic, formula-generated week of completed cycles — including Eco Mode relaxation, temperature samples, and vent events — into a fixed past window (default `2025-06-01 → 2025-06-07`). It requires developer mode, never touches real cycle history (only `demo-` prefixed rows are wiped on reseed), and the demo rows are exempt from the retention purge. Two consumers: instantly-populated charts on a fresh install, and the E2E visual-regression suite, which pins the Metrics page to that window under CI so the chart goldens are pixel-stable.

## Vent timeline disclosure

The vent-timeline chart shows the named vent-state-transition events the engine logs to `cycle_vent_events` — `opened_at_start`, `closed_reached_target`, `force_reopened_max_closed`, `reopened_min_runtime_hold`, `closed_overflow_hold`, `opened_overflow_hold`, and `reopened_drift` (a served room's vent reopening mid-cycle after drifting past its deadband, #503). Other vent movements are not currently tracked.

Note: the disclosure text the `/vent-timeline` endpoint itself returns (and that the chart renders below the table) still lists only the first six event types — it predates `reopened_drift` and undersells what's tracked. Rely on the list above until `routes.py`'s note string is updated to match.
