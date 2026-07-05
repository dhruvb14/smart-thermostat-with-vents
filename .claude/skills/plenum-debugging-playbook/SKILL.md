---
name: plenum-debugging-playbook
description: Symptom-to-triage playbook for Plenum (smart-thermostat-with-vents). Load when something is behaving wrong at runtime and you need to find the cause fast — temperatures off by ~×1.8 or ~30°, cycles that never end or end early, vents thrashing or stuck open/closed, a zone the engine silently stopped controlling, timestamps shifted by hours, duplicated/zombie live-feed rows, HA entity or cover-service failures, wrong heat/cool mode or oscillation, or one of the three classic CI failures. Gives ranked causes, the exact discriminating command/query/log line, and the issue precedent for each.
---

# Plenum Debugging Playbook

Symptom → ranked causes → one discriminating experiment → precedent. Built from
the 48 closed `label:bug` issues plus the engine-audit issues, verified against
the repo (as of 2026-07, v0.22.1).

**When NOT to use this skill**
- You need to *measure* something (log-mining scripts, metrics interpretation,
  DB query recipes, WS capture) → `plenum-diagnostics-and-tooling`.
- You want the full settled history of an incident (root cause → fix → status)
  → `plenum-failure-archaeology`.
- You are about to *change* code to fix what you found → `plenum-change-control`
  first (gates, evidence, non-negotiables).
- You don't understand a domain term (deadband, overshoot, short-cycling,
  dead-heading, hvac_action vs hvac_mode) → `hvac-zoning-reference`.
- CI failure beyond the three classics in family 8 → `plenum-ci-and-release`.

Jargon used below, once: **deadband** = tolerance band around a target inside
which a room is "close enough" (a *delta*, not an absolute temp). **overshoot
(delta)** = how far past the target the commanded setpoint is pushed so the
HVAC actually fires. **tick** = the engine's 60 s evaluation pass. **golden** =
committed reference screenshot for visual-regression E2E.

---

## Where to look — the diagnostic anatomy

All paths relative to repo root; runtime paths are inside the container.

| Surface | What it is | How to read it |
|---|---|---|
| Backend log | Python logging → container stdout. Every event-log write is mirrored here as `[CATEGORY] message` (`smart_vent/backend/event_logger.py:71`) | Docker: `docker logs <container>`. HAOS: add-on Log tab (container name carries the repo-ID prefix, e.g. `..._plenum` — see #92) |
| `event_log` DB table | Persisted structured events: `id, timestamp, level, category, message, details` (`db.py`). Categories in use: `system`, `api`, `engine`, `presence`, `ha`, `reconcile`. Capped ~5000 rows (trim had an off-by-one, #299) | `sqlite3 /data/app.db "SELECT timestamp,level,category,message FROM event_log ORDER BY id DESC LIMIT 50;"` (DB is `$DATA_DIR/app.db`, default `/data/app.db` — `backend/main.py:42-43`) |
| Logs page → Live Feed | UI over `GET /api/logs/events` + WS pushes | Filter by category/level; beware #302 dup rows, #283 zombie sockets |
| Logs page → Cycle History | UI over `GET /api/logs` (cycle_logs rows), per-cycle drill-down `GET /api/logs/{cycle_id}/detail` and `/temp-samples` | A cycle with `ended_at IS NULL` shows "Active/running" |
| Cycle tables | `cycle_logs`, `room_cycle_states` (per-room `target_temp, reached_at, vent_closed_at, role, joined_at`), `cycle_temp_samples`, `cycle_setpoint_history`, `cycle_vent_events` | `sqlite3 /data/app.db "SELECT id,mode,started_at,ended_at,ended_reason,in_min_runtime_hold FROM cycle_logs ORDER BY started_at DESC LIMIT 10;"` |
| HA states as Plenum sees them | `POST /api/ha/states` with `{"entity_ids": [...]}` — sensor values already normalized to °F | `curl -s -X POST localhost:8099/api/ha/states -H 'Content-Type: application/json' -d '{"entity_ids":["sensor.x"]}'` |
| Zone status | `GET /api/status` (per-zone cycle_state/hvac_mode/current_temp/setpoint), `GET /api/system/status`, `GET /api/healthz` | The #367 evidence format |
| Health endpoints | `GET /api/sensor-health` (per-sensor freshness vs staleness threshold), `GET /api/thermostat-health` | First stop for "engine seems blind" |
| Settings | `GET /api/settings`; `system_settings` table keys include `temperature_unit`, `unit_change_ack_required`, `unit_change_acked_unit`, `system_enabled`, `developer_mode`, `sensor_stale_after_min`, `vacation_mode_enabled`, `vacation_mode_return_at`, `outside_temperature_entity_id`, `mcp_enabled` | `sqlite3 /data/app.db "SELECT * FROM system_settings;"` |
| Live WS | `GET /ws` (`main.py:146`), broadcasts zone status + log events, temperatures raw °F | |

---

## Master triage table

| # | Symptom (as an operator states it) | Most likely causes, ranked | Discriminating experiment | Precedent |
|---|---|---|---|---|
| 1 | "Shows 158°F / -16°C / values off by ×1.8 or ~30°" | (a) absolute-vs-delta conversion misuse; (b) double conversion (frontend converted before POST); (c) a write path bypassing `to_f` (MCP, new endpoint); (d) HA fixture/instance in the wrong unit | Read the stored value: `sqlite3 /data/app.db "SELECT min_setpoint,deadband FROM thermostat_configs;"` — sane °F (60–85, deadband ~0.5–2) ⇒ display bug; insane (141.44, negative) ⇒ write-boundary bug | #231 #280 #281 #284 #291 #292 |
| 2 | "Cycle runs for hours / never ends" or "ends the instant it starts" | (a) post-closure drift blocking termination; (b) large `temp_offset` / stale sensor lying about room temp; (c) deadband in the completion check; (d) schedule window restarting cycles | `curl -s localhost:8099/api/logs/<cycle_id>/detail` — compare `vent_closed_at` per room vs cycle `ended_at`; then `GET /api/sensor-health` for staleness | #86 #70 #211 #38 |
| 3 | "Vents open/close repeatedly" or "stay open/closed when they shouldn't" | (a) min-runtime hold not gated (thrash); (b) close path bypassing `control_method` dispatcher (silently no-op close); (c) idle-room vents left over from previous cycle; (d) airflow floor (`min_open_vents_fraction`) refusing a close | `sqlite3 /data/app.db "SELECT timestamp,entity_id,action,reason FROM cycle_vent_events WHERE cycle_id='<id>' ORDER BY timestamp;"` — alternating open/close on the same entity ⇒ thrash; "Closed" logged but HA shows open ⇒ dispatcher bypass | #237 #57 #82 #67 #244 #210 |
| 4 | "The engine just stopped doing anything for a zone" | (a) exception before `engine.tick()` swallowed; (b) engine deleted mid-cycle (last room removed); (c) startup task garbage-collected; (d) `active_rooms=0` early-exit (no safety checks run); (e) thermostat unavailable mid-cycle | Backend log: is the 60 s `Reconcile <climate.x>: engine=... active_rooms=N` line still appearing for that zone? Absent ⇒ (a)/(b)/(c). Present with `active_rooms=0` while rooms bake ⇒ (d) | #286 #285 #304 #367 #267 |
| 5 | "Times are hours off / picker rejects valid times" | (a) `datetime.now()` (naive local) mixed into a UTC pipeline; (b) frontend printing raw naive-UTC string without `+ "Z"` conversion; (c) UTC used to build a `datetime-local` bound; (d) wrong `timezone` add-on option / `TZ` env | Offset direction test: shifted by exactly the browser's UTC offset ⇒ display bug (b); shifted the *other* way ⇒ backend wrote local time (a). Check `echo $TZ` in the container vs the `timezone` option | #65 #26 #294 #301 |
| 6 | "Live feed shows duplicates / keeps loading after I leave / Clear doesn't work" | (a) WS reconnect after intentional close (zombie sockets, stale handlers); (b) offset pagination sliding under new rows; (c) local-only Clear repopulated by polling | Browser devtools → Network → WS: more than one open `/ws` connection after navigating away and back ⇒ (a). Duplicated rows only after "Load older" ⇒ (b) | #283 #302 #303 #297 (backend twin) |
| 7 | "Vent didn't move / sensor shows unavailable / HA connection flaky" | (a) vent's `control_method` doesn't match what the integration supports (`cover.open_cover` unsupported); (b) sensor stale-but-numeric; (c) HA WS busy-loop or hung handshake during HA restart | Test the vent directly: `POST /api/vents/test` (the Rooms-page Test Open/Close buttons use it); `GET /api/sensor-health` for (b); backend log connect/close spam for (c) | #57 #82 #211 #297 |
| 8 | "CI is red" (the 3 classics) | (a) golden screenshot diff; (b) `TEMPERATURE_FIELDS` parity test; (c) coverage gate | (a) look at the changed PNGs in the PR diff — visual-regression legs now live in `container-ci.yml` (not a standalone e2e.yml) and a `commit-goldens` fan-in pushes regenerated goldens; (b) run `python -m pytest backend/tests/test_temperature_field_parity.py -v` from `smart_vent/`; (c) `fail_under = 92.5` in `pyproject.toml`, vitest lines 90 / functions 85 / branches 72 / statements 87 in `frontend/vite.config.ts` | #182 #369 #329; → `plenum-ci-and-release` |
| 9 | "It's heating when it should cool" / "heat and cool alternate every few minutes" | (a) mode inferred from live `hvac_action` at cycle boundary (heat_cool oscillation); (b) `_read_hvac_mode` fallback defaulting to heat on `idle`; (c) big `temp_offset` or stale sensor flipping the room-temp vote; (d) restored stale cycle mode after restart | `sqlite3 /data/app.db "SELECT started_at,mode,ended_reason FROM cycle_logs ORDER BY started_at DESC LIMIT 10;"` — strict cool/heat alternation with minutes-long gaps ⇒ (a); one wrong-direction cycle after restart ⇒ (d) | #29 #38 #48 #26 |

---

## Family detail and time-sink stories

### 1. Temperatures wrong by ~×1.8 or ~30° (unit conversion)

The conversion contract is owned by `plenum-architecture-contract` / CLAUDE.md;
one line here: **storage is always °F; the backend converts on write
(`units.to_f` / `units.delta_to_f`), the frontend converts on display only.**
The helpers live in `smart_vent/backend/units.py` (re-exported into
`routes.py`; CLAUDE.md corrected 2026-07-05 to match).

Discriminate the four signatures:

| Signature | Meaning | Precedent |
|---|---|---|
| Stored value ≈ user's °C × 1.8² + offsets (16 → 141.44) | **double conversion** — frontend called `toStorage` before POST *and* backend `to_f` converted again | #231 |
| Stored value = raw user °C (21 stored as 21 °F) | write path **bypassed** `to_f` entirely — check MCP tools and any new endpoint against `TEMPERATURE_FIELDS` (`routes.py:243` + `e2e/tests/temperature-fields.ts`) | #284 |
| Display shows a **negative °C** for a small positive delta (2 °F deadband → −16.7 °C) | **absolute conversion applied to a delta** — someone used `toDisplay`/`from_f` (subtracts 32) where `toDisplayDelta`/`from_f_delta` (scale only) was required | #291; magnitude-only ×1.8 error with no sign flip = delta never converted at all, #292 |
| Everything is °F even though the home is metric | auto-detect broken: HA `unit_system` is an *object* (compare `unit_system["temperature"] == "°C"`, not `== "metric"`), and blank `temperature_unit` in `run.sh` used to default-lock to `F` | #281; engine reading raw °C climate attributes as °F = #280 (runaway-HVAC class, not display) |

Story (#231, the costliest): a Celsius user typed 16 °C; it reached the DB as
141.44 °F. Both sides' unit tests passed, because each side's conversion was
individually correct — only the *composition* was wrong. That escape created
the three-file parity system and the dual-unit E2E matrix. If you see family-1
symptoms, your first question is always "which side converted, and how many
times?" — answered by reading the stored °F value, never by reading the UI.

### 2. Cycle never terminates / terminates early

- **Never terminates (#86):** after a room's vent closes, its temperature
  drifts back across the target on the very next tick; if the completion check
  counts closed-vent rooms as "not at target", termination is blocked forever.
  Rule that came out of it: *a room whose `vent_closed_at` is set has been
  served; its later drift is irrelevant.* Check `room_cycle_states`: all rooms
  have `vent_closed_at` set but the cycle's `ended_at` is NULL ⇒ this class.
- **Terminates/marks-at-target too early (#70):** deadband belongs to the
  *start* decision, not the completion check. If rooms are declared done at
  `target + deadband`, look at `_is_at_target` usage.
- **Stale sensors (#211):** a battery Zigbee sensor that dropped off the mesh
  keeps its last numeric value in HA — engine happily controls on 3-hour-old
  data. Guard exists now: `get_numeric_state(entity_id, max_age_min=...)`
  treats readings older than the threshold as `None`
  (`ha_client.py:155`); threshold is the `sensor_stale_after_min` system
  setting (default `SENSOR_STALE_AFTER_MIN = 30.0`, `cycle_engine.py:55`),
  tunable via `PUT /api/settings/sensor-staleness`, visible via
  `GET /api/sensor-health`. A stale episode emits a one-per-episode warning
  in the Live Feed.
- **`temp_offset` (per-room, delta, rooms table):** a large offset can hold a
  room artificially far from target. `SELECT name,temp_offset,deadband_override
  FROM rooms;`
- Also remember `cycle_timeout_hours` (default 3.0) is the backstop — a cycle
  that ends with `ended_reason` mentioning timeout was probably stuck in one of
  the above.

### 3. Vents thrash or stay open/closed wrongly

- **Thrash during min-runtime hold (#237):** after all active rooms hit target
  but before `min_cycle_runtime_min` elapses, the engine re-opens vents so the
  compressor has somewhere to blow — then the next monitor tick tried to
  re-close them. Fix persisted `cycle_logs.in_min_runtime_hold` as a gate;
  a room re-opened this way is recorded as a `reopened_min_runtime_hold`
  vent event and gets `role='overflow'` in `room_cycle_states`. If you see
  open↔close alternation in `cycle_vent_events` during a hold, the gate is
  being bypassed.
- **"Closed" in logs but open in HA (#57/#82):** vents have a per-vent
  `control_method` (`open_close` default, `set_position`,
  `set_tilt_position`, `toggle` — `room_vents.control_method`). Any close
  path that calls `ha.close_cover()` directly instead of
  `VentController._invoke_close` silently no-ops on position/tilt vents.
  Story: live logs proudly said "Closed idle room Bathroom vents" while every
  Flair vent stayed wide open — the engine and reality disagreed for entire
  cycles because the raw call sites were the two *idle-room* close paths,
  which no test exercised. Discriminator: `POST /api/vents/test` (or the
  Rooms-page Test buttons) against the same entity.
- **Idle-room vents wrong at cycle boundaries (#67/#244):** `_terminate_cycle`
  reopens all zone vents; a new cycle must then *close* non-participating
  rooms' vents (#67), and termination must reopen idle rooms it closed at
  start (#244 — pre-fix, the once-per-cycle `Drift (idle)` WARNING was just
  the reconciler cleaning this up, not a real fault). Current idle-reconciler
  message: `Vent <entity> found closed while zone is idle — re-opened.` plus
  "closed externally" guidance (`cycle_engine.py:2636`) — if you see it more
  than ~once per cycle, suspect an external actor (manual HA control,
  another automation), not the engine.
- **Airflow floor:** the count-based `min_open_vents` was replaced by
  `min_open_vents_fraction` (default 0.333) + `total_vents_count` +
  `has_bypass_damper` on `thermostat_configs` (#213 wave, dead-head
  protection #210). A vent that "refuses" to close may be the floor working
  as designed — check the event log reason before filing a bug.

### 4. Engine silently stops controlling a zone

The nastiest family because the failure is *absence of logs*.

- **#286:** `_tick_engine` had DB calls *outside* its try block; a
  `database is locked` there propagated into a `gather(...,
  return_exceptions=True)` whose result was discarded — zero output, zone
  dead. Discriminator: the per-tick `Reconcile <entity>: engine=...` line
  (category `reconcile`, `cycle_engine.py:2435`) stops appearing for that
  zone while other zones keep logging.
- **#285:** deleting the last room of a thermostat deleted its engine mid-cycle
  with no abort — HA kept the overshoot setpoint indefinitely, cycle row stuck
  `ended_at IS NULL`. If a zone vanished from the UI but HA is still
  heating/cooling toward a weird setpoint, check for an orphan open cycle:
  `SELECT id,thermostat_entity_id,started_at FROM cycle_logs WHERE ended_at IS NULL;`
- **#304:** fire-and-forget startup tasks held only by weak refs could be
  GC'd — unit resolution silently never ran. Suspect this only right after
  startup.
- **#367:** `active_rooms=0` early-return skipped *all* safety checks —
  `max_setpoint` was only evaluated on the active-room path, so an upstairs
  zone sat at 81 °F with a 77 °F "hard cap" for 2+ hours, every tick calmly
  logging `active_rooms=0`. Lesson: a safety cap that lives inside the comfort
  path is not a safety cap. Reconcile lines *present* + rooms out of bounds ⇒
  this class.
- **#267:** thermostat `unavailable` mid-cycle used to suspend every safety
  monitor forever. Now bounded by `thermostat_configs.unavailable_abort_after_min`
  (default 5); check `GET /api/thermostat-health`.

### 5. Timezone-shifted times

Backend rule (see `backend/tz.py` docstring): **store UTC, convert to local
only at display/aggregation**, local zone comes from the `timezone` add-on
option exported as `TZ` by `run.sh` (line 40/81; option default
`America/New_York` in `config.yaml`). Naive-UTC ISO strings cross the API; the
frontend appends `"Z"` before `new Date(...)`.

Ranked causes, with direction as the discriminator (browser at UTC−5):
1. Time shown 5 h *early* → backend wrote **local** naive time and frontend
   added "Z" (double shift) — the #65 `datetime.now()`-in-`room_manager`
   pattern.
2. Time shown 5 h *late* (i.e. raw UTC) → frontend printed the naive-UTC
   string verbatim without the `+ "Z"` conversion — #26 (Dev Mode), #301
   (vent timeline).
3. A `datetime-local` picker rejecting valid near-future times → its
   `min`/bound built with `toISOString()` (UTC) instead of local components —
   #294 (vacation modal).
4. Schedules firing at wrong wall-clock times → wrong `TZ` in the container:
   `docker exec <c> printenv TZ` and compare with the add-on option; all local
   time must flow through `tz.get_timezone()`.

### 6. WS / live-feed weirdness

- **#283 (frontend):** `connectWS`'s close listener unconditionally scheduled a
  reconnect, so an *intentional* dispose resurrected the socket with the old
  handler — N filter changes ⇒ each event rendered N+1 times, and every
  Dashboard visit left a zombie re-fetching 4 endpoints per broadcast forever.
  Current `api.ts` `connectWS` (~line 863) tracks a `closed` flag; if dup rows
  return, check that flag logic first, then devtools → count open `/ws`
  connections.
- **#302:** offset pagination + newest-first feed + live inserts ⇒ "Load older"
  refetches rows you already have (React duplicate-key warnings in console are
  the tell).
- **#303:** Dev Mode "Clear" only cleared local state; the 3 s poll of
  `/api/logs/events?category=dev` repopulated it.
- **#297 (backend twin):** HA WS client busy-looped on clean close (no backoff
  reset guard) and could hang forever in an unguarded handshake `receive_json`
  — symptom: connect/auth/subscribe log spam during an HA restart window, or
  an add-on that never reconnects until restarted.

### 7. HA connection / entity problems

- Unsupported cover service → family 3 (#57/#82): fix is per-vent
  `control_method`, verified with `POST /api/vents/test`; a vent failure is
  logged as an `error` event (category `engine`) and never aborts the cycle.
- Sensor `unavailable`/`unknown` → `get_numeric_state` returns `None` (room
  skipped / thermostat-ambient fallback); sensor *stale but numeric* → family
  2 (#211), check `GET /api/sensor-health`.
- Thermostat entity unavailable → #267 abort-after-N-minutes; transient blips
  are tolerated by design.
- HA restart window → #297 reconnect pathologies; also #38 failure mode 1:
  right after (re)connect the state cache can be empty, so mode inference sees
  no readings — the engine falls back to thermostat ambient rather than
  returning "off".
- `POST /api/ha/states` (`routes.py:1364`) is the "what does Plenum think HA
  says" oracle — sensor values in the response are already normalized to °F,
  so comparing it with HA's own Developer Tools (which shows native units) is
  itself a unit-conversion check.

### 8. CI failures — the three classics (detail lives in `plenum-ci-and-release`)

1. **Golden diff.** Any rendered-UI change fails the visual-regression legs
   (inside `.github/workflows/container-ci.yml`; the standalone `e2e.yml`
   is gone — CLAUDE.md corrected 2026-07-05). The matrix legs regenerate goldens and a
   `commit-goldens` fan-in job pushes them back (`git push origin
   HEAD:"$BRANCH"` — the detached-HEAD form, #369). Time-varying UI must be
   wrapped in `<Frozen>` (`frontend/src/ci.tsx`) or goldens never stabilize
   (#182). Review changed PNGs like code.
2. **Parity test.** `smart_vent/backend/tests/test_temperature_field_parity.py`
   fails if `TEMPERATURE_FIELDS` in `routes.py` and
   `e2e/tests/temperature-fields.ts` diverge, a `kind` mismatches, or a
   `ui: true` field lacks a `// @covers:` tag in
   `e2e/tests/temperature-units.spec.ts`. It is telling you a #231-class bug
   is possible — add the missing entry, don't weaken the test.
3. **Coverage gate.** Backend `fail_under = 92.5` (`smart_vent/pyproject.toml`);
   frontend vitest thresholds lines 90 / functions 85 / branches 72 /
   statements 87 (`smart_vent/frontend/vite.config.ts`). CLAUDE.md's older
   numbers (90 / 80.9…) are stale — trust the repo files.
   Also common but flake-class: the E2E global-setup room-creation click race
   (#329) — a red Round-trip leg on an unrelated PR is usually this; re-run
   before digging.

### 9. Wrong HVAC mode / oscillation

- **#29 (the classic):** on `heat_cool` thermostats, deriving each new cycle's
  direction from live `hvac_action` at the cycle *boundary* oscillates: cool
  to 68, terminate, thermostat now below its own heat setpoint reports
  `heating`, engine heats to 72, repeat — HVAC fighting itself. Fix direction
  from **room temps vs targets** (`_infer_mode_from_room_temps`), and if all
  rooms are within deadband, start nothing.
- **#26 sub-bug 1a:** `hvac_action` goes `idle` *exactly when* the room reaches
  target (it satisfied its own setpoint); a fallback that maps idle-on-
  heat_cool to "heating" inverts the at-target comparison at precisely the
  moment it matters, so vents never close. Any mode-fallback that "defaults to
  heat" (#48 bug 2) is a red flag in review.
- **#38:** no ambient cross-check — a `temp_offset = -4` room or a stale sensor
  can vote "heat" while the thermostat itself reads 74 °F against a 72 °F
  target. Also post-restart: `restore_from_db` used to resurrect a stale cycle
  mode unvalidated.
- **#48 bug 1:** correct mode but HVAC never fires — setpoint computed only
  from room targets can land on the satisfied side of the *thermostat's* own
  ambient (hallway probe cooler than bedrooms). The setpoint must be clamped
  strictly beyond thermostat ambient.
- Mode is **locked per cycle** (`_cycle_mode`) — mid-cycle flips are corrected
  by reconciliation (watch for `Drift: thermostat ... mode changed` events).
  Oscillation therefore always lives at cycle *boundaries*; check
  `cycle_logs.mode` alternation, not mid-cycle state.

---

## Provenance and maintenance

Date-stamped facts (all verified against the repo on 2026-07-04, v0.22.1) and
how to re-verify:

- Table names / schemas: `grep -n "CREATE TABLE" smart_vent/backend/db.py`
- Endpoints: `grep -n "@routes\." smart_vent/backend/api/routes.py`
- Unit helpers in `backend/units.py` (not routes.py): `grep -n "def " smart_vent/backend/units.py`
- `TEMPERATURE_FIELDS` at `routes.py:243` and `e2e/tests/temperature-fields.ts` (line numbers drift; grep the name)
- Staleness default 30.0 min: `grep -n "SENSOR_STALE_AFTER_MIN" smart_vent/backend/engine/cycle_engine.py`
- `unavailable_abort_after_min` default 5, `min_open_vents_fraction` default 0.333: thermostat_configs DDL in `db.py`
- Coverage gates: `grep fail_under smart_vent/pyproject.toml`; thresholds block in `smart_vent/frontend/vite.config.ts`
- Visual regression + round-trip legs inside `container-ci.yml`: `grep -n "visual\|Round-trip\|commit-goldens" .github/workflows/container-ci.yml`
- WS route `/ws`: `grep -n add_get smart_vent/backend/main.py`
- `TZ` from `timezone` option: `grep -n TZ smart_vent/run.sh`
- Event-log cap ~5000 rows and categories: `smart_vent/backend/db.py` (`_EVENT_LOG_MAX`) and `event_logger.py`
- Issue bodies quoted from the GitHub issue tracker (`dhruvb14/smart-thermostat-with-vents`), mined 2026-07-04; issue numbers are stable, quoted line numbers inside them are not.
