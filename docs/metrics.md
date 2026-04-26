# Metrics & analytics

The **Metrics** page (left nav → *Metrics*) shows how each thermostat — and the home as a whole — has been running. Default view is the last 7 days, switchable to any date range.

## Layout

| Section                       | What it shows                                                                                                   |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Outside-temperature picker    | Single system-wide HA entity Plenum reads at every cycle start/end. Live value displayed alongside.             |
| Filters                       | Thermostat selector ("All thermostats" by default), date-range picker, "Last 7 days" reset, **Export CSV**.     |
| Summary tiles                 | Heating time, cooling time, duty cycle %, cycle count + completed count, average outside temperature.           |
| Charts (per-thermostat view)  | All 14 charts listed below.                                                                                     |
| Charts (home view)            | Completion-rate and source-breakdown donuts only — the per-thermostat charts only make sense for a single zone. |

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

A daily APScheduler job rolls completed cycles into `daily_thermostat_metrics` at 00:05 local; a monthly job rolls them into `monthly_thermostat_metrics` at 00:10 on the 1st. The page itself queries `cycle_logs` directly so "today" is always live; the rollup tables back longer-horizon trends.

Manual triggers (useful during testing or after a restore):

- `POST /api/metrics/rollup/daily   {"days_back": 7}`
- `POST /api/metrics/rollup/monthly {"months_back": 3}`

## Vent timeline disclosure

The vent-timeline chart shows **cycle-boundary** events only — `opened_at_start`, `closed_reached_target`, `force_reopened_max_closed`, `closed_at_end`. Mid-cycle vent movements are not currently tracked.
