---
name: plenum-diagnostics-and-tooling
description: How to MEASURE Plenum's runtime behavior instead of eyeballing it — the SQLite app.db schema with copy-pasteable diagnostic queries, the Logs page (Live Feed / Cycle History) and event-logger categories, what every Metrics chart actually computes (duty cycle, degree-minutes, overshoot histogram, time-to-target), the live-state endpoints and /ws stream, and ready-to-run scripts (cycle_report, overshoot_stats, event_tail, hvac_quality, ws_watch) for cycle reports and HVAC-quality analysis (short-cycling, oscillation, chronic overshoot, drifting rooms). Load when you need numbers, history, or evidence about what the system did.
---

# Plenum diagnostics and tooling

This skill is about **measurement**: turning "the bedroom feels wrong" into a
query, a chart interpretation, or a script run that yields a number. Plenum
persists a rich diagnostic trail in SQLite — nearly every question about past
behavior is answerable without touching the running process.

**When NOT to use this skill:**

- You have a *symptom* and need ranked causes → `plenum-debugging-playbook` (it owns triage; this skill owns the instruments it tells you to point at things).
- You need the °F-storage / write-boundary contract → `plenum-architecture-contract`.
- You need what a config knob does or its default → `plenum-config-and-flags`.
- You need domain theory (what overshoot/short-cycling physically are) → `hvac-zoning-reference`.
- You need to *run* the app or find where it's deployed → `plenum-run-and-operate`.

Jargon used below (one-line definitions; theory in `hvac-zoning-reference`):
**cycle** = one engine-driven HVAC run from setpoint-command to termination;
**deadband** = tolerance band around target within which a room counts as "at target";
**overshoot** = how far past target a room's temperature went;
**short cycle** = HVAC run too brief for the compressor's health;
**holdover** = presence-triggered activity window that outlives the last motion event;
**duty cycle** = fraction of wall-clock time HVAC was running;
**degree-minutes** = ∫|setpoint − thermostat temp| dt, an "unmet demand" integral.

---

## 1. The SQLite DB as diagnostic surface

### Where it is and how to open it

- Path: `${DATA_DIR}/app.db`. `DATA_DIR` defaults to `/data` in the backend
  (`smart_vent/backend/main.py:42-43`) and is exported as `/config` by the
  add-on's `run.sh` (as of 2026-07, v0.22.1). Local dev: wherever you pointed
  `DATA_DIR`.
- WAL mode is on (`PRAGMA journal_mode=WAL` in the schema). Reading a live DB
  is safe; if you copy it elsewhere, copy `app.db-wal` and `app.db-shm` too or
  you lose the most recent writes.
- **Never write to a production DB.** Open read-only:
  ```bash
  sqlite3 "file:/config/app.db?mode=ro"
  ```
  If the `sqlite3` CLI is unavailable (it is not in every container), Python's
  stdlib works everywhere:
  ```bash
  python3 -c "import sqlite3,sys; c=sqlite3.connect('file:/config/app.db?mode=ro',uri=True); [print(r) for r in c.execute(sys.argv[1])]" "SELECT key,value FROM system_settings"
  ```
  All scripts in this skill's `scripts/` dir open `mode=ro` and take the DB
  path as their first argument.

### Conventions that will bite you if unknown

| Convention | Detail |
|---|---|
| Temperatures | **Always stored °F** (absolute and delta alike). See `plenum-architecture-contract` for the write-boundary contract. |
| Timestamps | Naive **UTC** ISO strings (`2026-07-04T11:48:00.123456`) — `db._dts()` strips tzinfo on write. Bucket to local time with SQLite's `date(col,'localtime')` like the metrics code does. Exception: `schedules.expires_at` is naive **local** wall-clock (#359). |
| Durations | Not stored; derive with `(julianday(ended_at)-julianday(started_at))*86400.0` seconds — the same expression `db.py` uses. |
| Running cycle | `cycle_logs.ended_at IS NULL`. |
| JSON columns | `cycle_logs.rooms_json` (`{room_id: {name, source}}`; `source` ∈ schedule/presence/override), `vents_at_start/_end`, `event_log.details`, `room_cycle_states.trigger_detail`. |

### Schema map (tables you'll actually query — full DDL in `smart_vent/backend/db.py` `SCHEMA` + `_MIGRATIONS`, lines 38–425)

| Table | Grain | Key columns |
|---|---|---|
| `cycle_logs` | one HVAC cycle | `id`, `thermostat_entity_id`, `started_at`, `ended_at` (NULL=running), `mode` (heating/cooling), `ended_reason`, `rooms_json`, `setpoint_at_start/_end`, `thermostat_temp_at_start/_end`, `outside_temp_at_start/_end`, `vents_at_start/_end`, `in_min_runtime_hold` |
| `room_cycle_states` | one room's participation in one cycle | PK `(cycle_id, room_id)`, `target_temp`, `reached_at`, `vent_closed_at`, `temp_at_start/_end`, `joined_at` (NULL = joined at cycle start), `role` (`active` \| `overflow` #254), `trigger_detail` |
| `cycle_temp_samples` | one temp reading per engine tick (60 s) per active room | `cycle_id`, `room_id`, `timestamp`, `room_temp`, `thermostat_temp`, `setpoint` |
| `cycle_setpoint_history` | one setpoint command | `cycle_id`, `timestamp`, `setpoint`, `reason` |
| `cycle_vent_events` | one cycle-boundary vent action | `cycle_id`, `timestamp`, `entity_id`, `room_id`, `action`, `reason` |
| `event_log` | one Live-Feed row (ring buffer, max 5000 rows, trimmed every 100 inserts) | `id`, `timestamp`, `level`, `category`, `message`, `details` (JSON) |
| `room_overrides` | one active manual override per room | PK `room_id`, `target_temp`, `expires_at` |
| `presence_holdover_state` | one active holdover per room | PK `room_id`, `last_detected_at`, `expires_at` |
| `daily_thermostat_metrics` / `monthly_thermostat_metrics` | per-thermostat rollups (#85) | `date`/`month`, `heating_seconds`, `cooling_seconds`, `cycle_count`, `completed/timeout/aborted_count`, `avg_cycle_duration_seconds` |
| `system_settings` | key/value flags | see `plenum-config-and-flags` Layer 3 for the key catalog |
| `rooms`, `room_sensors`, `room_vents`, `room_presence_sensors`, `schedules`, `thermostat_configs` | configuration | see `plenum-config-and-flags` |

### Copy-pasteable queries (all verified against a DB built from the real DDL)

Recent cycles with durations:
```sql
SELECT id, mode, started_at, ended_at, ended_reason,
       ROUND((julianday(ended_at)-julianday(started_at))*1440.0,1) AS dur_min
FROM cycle_logs ORDER BY started_at DESC LIMIT 20;
```

Currently-running cycle(s):
```sql
SELECT id, thermostat_entity_id, mode, started_at FROM cycle_logs WHERE ended_at IS NULL;
```

Per-room reached/closed timing for one cycle (NULL `reached_at` = never hit target):
```sql
SELECT rcs.room_id, rcs.target_temp, rcs.temp_at_start, rcs.temp_at_end, rcs.role,
       rcs.reached_at, rcs.vent_closed_at, rcs.joined_at
FROM room_cycle_states rcs WHERE rcs.cycle_id = '<CYCLE_ID>';
```

Setpoint history for a cycle (every engine setpoint command with reason):
```sql
SELECT timestamp, setpoint, reason FROM cycle_setpoint_history
WHERE cycle_id = '<CYCLE_ID>' ORDER BY timestamp;
```

Temperature trajectory for a cycle (one row per 60 s tick per active room):
```sql
SELECT timestamp, room_id, room_temp, thermostat_temp, setpoint
FROM cycle_temp_samples WHERE cycle_id='<CYCLE_ID>' ORDER BY timestamp;
```

Vent events for a cycle:
```sql
SELECT timestamp, entity_id, room_id, action, reason
FROM cycle_vent_events WHERE cycle_id='<CYCLE_ID>' ORDER BY timestamp;
```

Warnings/errors from the engine (event_log by category):
```sql
SELECT timestamp, level, message FROM event_log
WHERE category = 'engine' AND level IN ('warning','error')
ORDER BY id DESC LIMIT 20;
```

Active overrides and presence holdovers (timestamps are UTC, so compare against UTC now):
```sql
SELECT o.room_id, r.name, o.target_temp, o.expires_at
FROM room_overrides o LEFT JOIN rooms r ON r.id = o.room_id
WHERE o.expires_at > strftime('%Y-%m-%dT%H:%M:%S','now');

SELECT h.room_id, r.name, h.last_detected_at, h.expires_at
FROM presence_holdover_state h LEFT JOIN rooms r ON r.id = h.room_id;
```

System-settings dump:
```sql
SELECT key, value FROM system_settings ORDER BY key;
```

Daily rollups (empty until the 00:05-local APScheduler job or a manual
`POST /api/metrics/rollup/daily` has run):
```sql
SELECT * FROM daily_thermostat_metrics ORDER BY date DESC LIMIT 7;
```

### `ended_reason` taxonomy (verified in `cycle_engine.py`, as of v0.22.1)

- `completed` — cycle reached its goal (`_terminate_cycle` default).
- `timeout` — hard `cycle_timeout_hours` hit (`_terminate_cycle(reason="timeout")`).
- `aborted: <reason>` — `_abort_cycle` prefixes `aborted: `; reasons seen in
  code: `thermostat unavailable`, `system disabled`, `vacation mode`,
  `no active rooms`, `no compatible rooms after filtering`.
- Metrics bucket these with `_ROLLUP_REASON_BUCKETS` (`db.py:1407`):
  completed = exact `completed`; timeout = `timeout` OR `aborted: timeout%`;
  aborted = other `aborted:%`.

### `cycle_vent_events.action` values (verified emit sites in `cycle_engine.py`)

`opened_at_start`, `closed_reached_target`, `force_reopened_max_closed`,
`reopened_min_runtime_hold`, `closed_overflow_hold`, `opened_overflow_hold`
(the last three are the #237/#254 overflow-conditioning actions).
Note: `closed_at_end` appears in `models.py`'s comment and the
`/api/metrics/.../vent-timeline` disclosure note, but **no emit site exists in
the engine as of v0.22.1** — don't expect those rows in fresh data.

---

## 2. The Logs page and the event logger

The **Logs** page (`frontend/src/pages/Logs.tsx`) has three tabs: **Live
Feed**, **Cycle History**, **Retention**.

### Live Feed = `event_log` + `/ws`

`EventLogger.log(level, category, message, details)`
(`backend/event_logger.py`) writes an `event_log` row **and** broadcasts it as
a `log_event` WS frame — so the Live Feed and the DB table are the same data.
Rows never raise; a failed DB write still broadcasts.

Categories actually emitted (verified by grepping every `.log(` call site, v0.22.1):

| Category | Emitted from | Typical content |
|---|---|---|
| `engine` | cycle_engine, vent_controller, scheduler | cycle start/end/abort, setpoint moves, safety-guard trips, stale sensors |
| `system` | scheduler, routes | enable/disable, unit changes, log clears |
| `api` | routes | user mutations (rooms/schedules/config edited) |
| `ha` | main | HA connect/reconnect |
| `presence` | scheduler | presence activations / holdover expiries |
| `dev` | ha_client | Dev-Mode simulated HA calls |
| `reconcile` | cycle_engine | periodic reconciliation pass results |

The UI filter list matches: `Logs.tsx:858` `CATEGORIES = ["all","system","api","engine","presence","ha","dev","reconcile"]`. Levels: `info`/`warning`/`error`.

REST equivalent (same filters the UI uses):
```bash
curl 'http://<host>:8099/api/logs/events?category=engine&level=warning,error&limit=50'
```
Params: `limit`, `offset`, `category`, `since`, `until` (ISO UTC), `level` (comma-separated).

Retention: `event_log` is trimmed to **5000 rows** (`db.py:_EVENT_LOG_MAX`)
every 100th insert, plus a purge job driven by `event_log_retention_days`
(default 7; Retention tab / `GET,POST /api/settings/log-retention`). Cycle
logs purge via `cycle_log_retention_days` (default 30).

### Cycle History drilldown

- List: `GET /api/logs?limit=&offset=&since=&until=` → `cycle_logs` rows plus a
  `had_overflow` badge flag (true when any `room_cycle_states.role='overflow'`).
- Drilldown: `GET /api/logs/{cycle_id}/detail` → the cycle + per-room
  states (target, reached_at, vent_closed_at, trigger_detail, role) + all
  vent events + setpoint history.
- Chart data: `GET /api/logs/{cycle_id}/temp-samples[?room_id=]`.

Use the drilldown for: runaway cycles (long duration + `timeout`), rooms that
never reach target (`reached_at` NULL), and external-setpoint surprises
(setpoint history rows whose `reason` you don't recognize).

---

## 3. The Metrics page — what each chart computes

Charts live in `frontend/src/components/charts/MetricsCharts.tsx`; the math
lives in `smart_vent/backend/db.py` (`compute_*` functions); endpoint↔chart
mapping is in `docs/metrics.md`. All computations read `cycle_logs` live
(rollup tables only back long-horizon trends), consider **completed cycles
only** (`ended_at IS NOT NULL`), and bucket by **local date of `started_at`**.

| Metric | Computation (db.py) | Interpretation |
|---|---|---|
| Summary tiles / duty cycle % | `compute_thermostat_summary`: Σ cycle durations by mode ÷ range seconds ×100. Home view multiplies range by thermostat count so % stays ≤100. | Sustained duty >50% in mild weather → undersized system, fighting zones, or cycles that won't terminate. Compare against outside temp. |
| Cycles / avg duration / hours per day | `compute_thermostat_timeseries` (`metric=cycles`/`avg_duration`/`hours`) | Many short cycles = short-cycling (see §5). A sudden avg-duration jump often correlates with an outside-temp swing — check chart 4d before blaming the engine. |
| Cycles vs outside temp (scatter) | `compute_cycles_vs_outside_temp`: each completed cycle as `(outside_temp_at_start, duration_minutes)`, colored by mode | The house's thermal-response curve. Points far off the trend at the same outside temp = anomalies worth drilling into. Empty until an outside-temp entity is configured (no backfill). |
| Time-to-target | `_time_to_target_timeseries`: per cycle, MIN over rooms of `reached_at − COALESCE(joined_at, started_at)`, averaged per bucket; overflow rooms excluded | Measures the *fastest* room, not the slowest — a drifting room does NOT move this chart. Use `scripts/hvac_quality.py` §4 or per-room `avg_time_to_target_seconds` for laggards. |
| Completion rate (donut) | summary's `completed/timeout/aborted_count` via `_ROLLUP_REASON_BUCKETS` | High timeout share → rooms that can't reach target (closed dampers, weak airflow, unrealistic setpoints). High aborted share → check which `aborted:` reason dominates (query above). |
| Source breakdown (donut) | summary walks `rooms_json`, counting each distinct `source` (schedule/presence/override) once per cycle | Which trigger drives the HVAC — a cycle with both schedule and presence rooms counts once in each; the buckets are not disjoint and don't sum to cycle_count. |
| Per-room participation / heat-cool time | `compute_room_metrics`: participation = room's non-overflow cycle count ÷ total cycles; time = `COALESCE(vent_closed_at, ended_at) − COALESCE(joined_at, started_at)` | Participation near 0 for a room that should be active = its schedules/presence never fire (see the silent-zone family in `plenum-debugging-playbook`). |
| Degree-minutes | `_degree_minutes_timeseries`: trapezoid-free left-sum of \|setpoint − thermostat_temp\| over inter-sample gaps, **using thermostat-level samples (`room_id IS NULL`) only** | "How much conditioning was demanded." CAVEAT: as of v0.22.1 the engine's only `insert_cycle_temp_sample` call site (`cycle_engine.py:1098`) writes per-room rows — verify NULL-room rows exist (`SELECT COUNT(*) FROM cycle_temp_samples WHERE room_id IS NULL`) before trusting an empty chart. |
| Overshoot histogram | `compute_overshoot_histogram` (#290): per (cycle, room), worst wrong-side excursion vs target — cooling: `max(target − min_seen, 0)`; heating: `max(max_seen − target, 0)`. Room-level samples preferred, thermostat-level fallback per room. Last bin is open-ended. | Values are **deltas** — the UI converts with `toDisplayDelta` (no −32). Rendering them with the absolute formula was bug **#291**. Consistent multi-°F overshoot → raise deadband or check `temp_offset` (#86). |
| Hour heatmap | `compute_hour_heatmap`: each cycle spreads its seconds across the local hour cells it overlaps, 7×24 Mon–Sun grid | Conditioning at hours nobody scheduled → presence sensors firing (pets?) or overrides left active. |
| Vent timeline | `get_vent_events_in_range` | **Cycle-boundary events only** — mid-cycle movements aren't tracked; the UI shows that disclosure deliberately. |

### CSV export semantics

`GET /api/metrics/export.csv?scope=home|thermostat[&entity_id=][&start=&end=]`
→ one row per completed cycle. This is the **only** read path that converts
temperatures: headers are unit-labeled (e.g. `setpoint_at_start (°C)`) and
values pass through `_from_f()` in the active display unit. Every other
API/DB temperature you see is raw °F. `duration_seconds` is computed at
export time.

### Rollups

Daily job 00:05 local → `daily_thermostat_metrics`; monthly 00:10 on the 1st.
Manual: `POST /api/metrics/rollup/daily {"days_back": 7}` — idempotent,
range-replacing. Needed after a restore or a retention purge.

---

## 4. Live state — endpoints and the WS stream

| Endpoint | Returns |
|---|---|
| `GET /api/healthz` | liveness ping |
| `GET /api/status` | one entry per engine: `thermostat_entity_id`, `cycle_state` (idle/running/…), `hvac_mode`, `hvac_action`, `current_temp`, `setpoint`, `cycle_id`, `cycle_started_at`, per-room `{room_id, avg_temp, vent_states, presence_active}` — all temps °F |
| `GET /api/system/status` | `{enabled, dev_mode, mcp_enabled}` |
| `POST /api/ha/states` `{"entity_ids":[…]}` | HA state-cache snapshot; **°C sensor values are normalized to °F** in `numeric` (routes.py ~1379-1387) — `numeric` is always °F regardless of display unit |
| `GET /api/sensor-health` | rooms with stale temp sensors (#211): `age_seconds` per sensor, `reason` `stale` or `not_in_cache`; threshold = `sensor_stale_after_min` setting, default 30 min (`SENSOR_STALE_AFTER_MIN`, cycle_engine.py:55). Healthy rooms omitted. |
| `GET /api/thermostat-health` | unavailable climate entities (#267): `unavailable_seconds`, `abort_after_min`, `cycle_running`. Healthy thermostats omitted. |
| `GET /api/logs/events` | event_log query (see §2) |

Quick health sweep from a shell:
```bash
H=http://<host>:8099
curl -s $H/api/status | python3 -m json.tool
curl -s $H/api/sensor-health | python3 -m json.tool
curl -s $H/api/thermostat-health | python3 -m json.tool
```

### The `/ws` stream

Single push-only WebSocket at `GET /ws` (`api/ws_handler.py`, heartbeat
ping every 30 s). Event frame shape: `{"type": <event_type>, "data": {…}}`.
Types emitted (verified broadcast call sites, v0.22.1):

- `zone_status` — per-engine snapshot from `cycle_engine._maybe_broadcast`
  (thermostat id, hvac_mode/action, current_temp, setpoint, cycle_id,
  cycle_state); raw °F.
- `log_event` — every EventLogger row (§2).
- `system_enabled_changed` / `dev_mode_changed` / `mcp_enabled_changed`.

Watch it from a shell with the pure-stdlib client in this skill:
```bash
python3 scripts/ws_watch.py ws://<host>:8099/ws                # everything
python3 scripts/ws_watch.py ws://<host>:8099/ws --type zone_status
python3 scripts/ws_watch.py ws://<host>:8099/ws --raw          # full JSON
```
It answers server pings, auto-reconnects, and needs no pip installs. `ws://`
only (no TLS) — point it at the exposed 8099 port or a port-forward, not an
HTTPS ingress URL.

---

## 5. HVAC-quality analysis from cycle data

The four failure signatures worth scanning for, and where they live in the data:

| Signature | Data test | Usual fix knob |
|---|---|---|
| **Short cycling** | completed cycles with duration < ~10 min | `min_cycle_runtime_min` (#208); also check deadband too tight |
| **Rapid restarts** | gap between `ended_at` and next `started_at` < ~10 min on the same thermostat | `min_cycle_offtime_min` (#208), `overshoot_delta` too small |
| **Mode oscillation** | heating→cooling (or reverse) within <60 min | rooms with conflicting targets on one thermostat; deadbands; see the opposite-cycle tiers in `hvac-zoning-reference` |
| **Chronic overshoot / drift** | per-room avg of `temp_at_end` vs `target_temp` (sign-normalized by mode); `reached_at` NULL rate | overshoot → deadband/`temp_offset` (#86); never-reaches → airflow, vent mapping, sensor placement |

`scripts/hvac_quality.py` runs all four at once and exits 1 if any red flag
fires (cron-able). `scripts/overshoot_stats.py` reproduces the Metrics-page
overshoot histogram offline plus a per-room breakdown the API doesn't expose.

---

## 6. The scripts

All in `scripts/` next to this file. Pure stdlib (python3, no pip), DB opened
strictly read-only (`file:…?mode=ro` URI — verified a write attempt raises),
DB path is always the first positional argument. Tested against a throwaway
DB built from the real `db.py` DDL (SCHEMA + `_MIGRATIONS`) with 7 days of
synthetic cycles.

| Script | Usage | Output |
|---|---|---|
| `cycle_report.py` | `python3 cycle_report.py app.db [--days 7] [--thermostat climate.x] [--limit 50]` | Table of recent cycles (id, mode, start, duration or RUN, setpoint/temps °F, ended_reason); summary block (counts by reason bucket, completion rate, avg duration, duty cycle); per-room reached/vent-closed timing for the latest completed cycle. |
| `overshoot_stats.py` | `python3 overshoot_stats.py app.db [--days 7] [--bin-size 1.0] [--max-bins 6]` | Overshoot histogram (same math as `compute_overshoot_histogram`, ASCII bars), overshot %, avg/max °F-delta, per-room averages with a `chronic (avg > 2°F)` flag. |
| `event_tail.py` | `python3 event_tail.py app.db [-n 30] [--category engine] [--level warning,error] [--follow]` | `timestamp LEVEL [category] message details` lines; `--follow` polls by id every 2 s (safe on a live WAL DB). |
| `hvac_quality.py` | `python3 hvac_quality.py app.db [--days 7] [--short-min 10] [--offtime-min 10]` | The §5 four-signature scan with example offenders and RED FLAG lines; exit code 1 if any flag fired. |
| `ws_watch.py` | `python3 ws_watch.py ws://host:8099/ws [--type zone_status] [--raw]` | One line per WS event; handles pings, reconnects. Tested against a minimal RFC 6455 server (handshake, masked pong, close). |

Example (`hvac_quality.py` output shape):
```
[1] short cycles (<10 min): 43/109 (39%)
      cyc004 cooling  2026-06-27T16:46:00 9.0 min  (completed)
      RED FLAG: >20% short cycles — check min_cycle_runtime_min / deadband
[2] short off-times (<10 min between cycles): 51
[3] heat<->cool oscillations (mode flip within 60 min): 4
[4] per-room target performance:
      Bedroom   n=109  never_reached=24%  avg_miss=+0.14°F
3 red flag(s).
```

---

## Provenance and maintenance

All facts verified 2026-07-04 against v0.22.1 source. Re-verification commands
(repo root):

- Schema / DDL: `grep -n "CREATE TABLE" smart_vent/backend/db.py` (lines 42–235 as of 2026-07) and `_MIGRATIONS` (line 357). If tables/columns change, rebuild the scripts' expectations.
- Event categories: `grep -rnoP '\.log\(\s*"\w+",\s*"\w+"' smart_vent/backend --include="*.py" | grep -v tests` — currently engine, system, api, ha, presence, dev, reconcile; UI list at `smart_vent/frontend/src/pages/Logs.tsx:858`.
- `ended_reason` values: `grep -n '_abort_cycle(conn, reason=\|_terminate_cycle(conn, reason=' smart_vent/backend/engine/cycle_engine.py`.
- Vent-event actions: `grep -n 'insert_cycle_vent_event' smart_vent/backend/engine/cycle_engine.py` then read each call's action string. `closed_at_end` still has no emit site → if one appears, update §1.
- Degree-minutes NULL-sample caveat: `grep -rn insert_cycle_temp_sample smart_vent/backend --include='*.py' | grep -v tests` — one call site (per-room) as of v0.22.1; recheck whether a thermostat-level (`room_id=None`) writer was added.
- Metrics endpoints & chart list: `grep -n '@routes.get("/api/metrics' smart_vent/backend/api/routes.py`; `docs/metrics.md`.
- WS event types: `grep -rn "_broadcast(\|broadcast(" smart_vent/backend/scheduler.py smart_vent/backend/engine/cycle_engine.py`.
- Constants: event-log cap `grep -n _EVENT_LOG_MAX smart_vent/backend/db.py` (5000); staleness default `grep -n "SENSOR_STALE_AFTER_MIN" smart_vent/backend/engine/cycle_engine.py` (30.0); ports `grep -n 8099 smart_vent/config.yaml`.
- Scripts smoke test: build a throwaway DB by extracting `SCHEMA`/`_MIGRATIONS` from `db.py` via `ast.literal_eval`, seed a few cycles, and run each script against it (this is how they were validated; they were **not** run against a production DB).

Known limits (as of 2026-07): scripts' `--days` filters compare against UTC
`datetime.utcnow()` while Metrics buckets by local date — expect ±1-day edge
differences vs the UI. `ws_watch.py` is plaintext-only. The mini WS server
used for testing lived in a scratchpad and is not shipped; re-testing
`ws_watch.py` against a live add-on is the stronger check.
