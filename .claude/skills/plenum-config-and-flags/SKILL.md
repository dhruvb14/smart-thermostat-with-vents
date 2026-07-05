---
name: plenum-config-and-flags
description: Catalog of every configuration axis in Plenum — config.yaml add-on options, run.sh/backend environment variables, system_settings DB keys, ThermostatConfig fields, per-room settings, schedule/override fields — with defaults, units, validation guards, and UI location. Load when you need to know where a setting lives, what its default/bounds are, whether a value is stored in °F or as a delta, or when adding a new knob (config option, temperature field, or system flag) and need the parity checklist.
---

# Plenum configuration and flags

Every tunable in Plenum, layer by layer, with where it is stored, its default,
its guard, and where the user reaches it. All facts verified against the repo
at v0.22.1 (2026-07). Trust these tables only after running the re-verification
one-liners in Provenance if the repo has moved.

**When NOT to use this skill**
- How to *run* the add-on / docker / ports / backup → `plenum-run-and-operate`.
- WHY a guard exists (deadband physics, short-cycling, dead-heading) → `hvac-zoning-reference`.
- What CI gates a change must clear before merging a new knob → `plenum-change-control`.
- The #231 write-boundary conversion contract itself → `plenum-architecture-contract` (one-liner here: frontend sends raw display values; the backend converts at the write boundary via `to_f`/`delta_to_f`).

**Jargon used below**
- *absolute* temp: a real temperature; °C→°F uses `*9/5 + 32`.
- *delta* temp: a difference (deadband, offset); °C→°F uses `*9/5` only — no 32 offset. Mixing these silently corrupts data.
- *deadband*: ±°F tolerance within which a room counts as "at target".
- *overshoot*: how far past the most demanding room's target the thermostat is set to keep the HVAC running.
- *holdover*: how long a presence-activated room stays active after last motion.

**The universal invariant**: every temperature is stored in °F in SQLite.
Conversion helpers live in `smart_vent/backend/units.py` (`to_f`, `delta_to_f`,
`from_f`, `from_f_delta`); `routes.py` imports them as `_to_f` etc. (CLAUDE.md
was corrected 2026-07-05 to say so — the import aliases are in routes.py, the
implementations in `units.py`.) Validation bounds are applied **after**
normalization to °F, per the lesson recorded in `.jules/sentinel.md`
(2026-05-05): validating the raw input before conversion is meaningless when
the unit varies. The canonical band for user-facing targets is **40–90 °F on
the internal °F value**; setpoint clamps get 40–100 °F (verified in code, see
tables).

---

## Layer 1 — Add-on options (`smart_vent/config.yaml` → `run.sh`)

Stored by the HA Supervisor in `/data/options.json`; read by `run.sh` via
`bashio::config` with an uppercase-env-var fallback for plain Docker (helper
`get_config` in `run.sh`). Parity between `config.yaml` `options:` and `run.sh`
is enforced by `smart_vent/backend/tests/test_addon_config.py`.

| Option | Schema | Default | Exported as | Notes |
|---|---|---|---|---|
| `ha_url` | str | `""` | `HA_URL` | Blank + supervisor → `http://supervisor/core`; blank + no supervisor → `http://homeassistant.local:8123` |
| `ha_token` | str | `""` | `HA_TOKEN` | Blank → `SUPERVISOR_TOKEN` when available |
| `use_wss` | bool | `false` | `HA_USE_WSS` | truthy set: `1/true/yes` (parsed in `ha_client.py`) |
| `ssl_verify` | bool | `true` | `HA_SSL_VERIFY` | |
| `timezone` | str | `"America/New_York"` (config.yaml) / `UTC` (run.sh fallback) | `TZ` | Drives schedule evaluation (`backend/tz.py` reads `TZ`) |
| `temperature_unit` | str | `""` (auto-detect) | `TEMPERATURE_UNIT` | Non-empty `F`/`C` is a **hard override lock** in the scheduler; blank lets HA `/api/config` + last-known DB value decide. Do not default to `F` (#281) |

Ports/manifest facts (config.yaml): ingress on **8099**, MCP on **9099**
(both host-ports optional/blank by default), `DATA_DIR: /config` via the
`environment:` block, `map: [data, addon_config:rw]`. `run.sh` one-time
migrates `flair.db`/`app.db` from `/data` to `$DATA_DIR`. `CI=true` makes
`run.sh` self-kill after 10 s (smoke test).

## Layer 2 — Environment variables the backend reads

Grep-verified across `smart_vent/backend/` (excluding tests):

| Env var | Read in | Default | Purpose |
|---|---|---|---|
| `DATA_DIR` | `main.py`, `mcp_server.py` | `/data` (backend) — but `run.sh` exports `/config` | SQLite `app.db` location |
| `PORT` | `main.py` | `8099` | Web UI / API port |
| `MCP_PORT` | `main.py` | `9099` | HTTP MCP server port |
| `HA_URL`, `HA_TOKEN` | `ha_client.py`, `mcp_tools/ha_entities.py` | see Layer 1 | HA WebSocket/REST auth |
| `SUPERVISOR_TOKEN` | `ha_client.py` | — | Injected by HA supervisor |
| `HA_USE_WSS` | `ha_client.py` | `false` | `wss://` vs `ws://` |
| `HA_SSL_VERIFY` | `ha_client.py` | `true` | TLS verification |
| `TZ` | `tz.py` | — | Local timezone for schedules |
| `TEMPERATURE_UNIT` | `scheduler.py` (`_unit_override`) | `""` | Unit override lock (see Layer 1) |
| `CI` | `run.sh` only | — | Smoke-test self-exit |

Frontend build-time: `VITE_APP_VERSION` — baked from `config.yaml version:`;
the value `"CI"` flips `isCI` in `frontend/src/ci.tsx` (frozen UI for E2E
goldens). Not a runtime knob.

## Layer 3 — `system_settings` DB keys (key/value TEXT table)

Accessors: `db.get_system_setting` / `db.set_system_setting`
(`smart_vent/backend/db.py` ~line 2259). Exhaustive key list (grep-verified;
booleans stored as `"1"`/`"0"`):

| Key | Default | Set by | Read by | UI |
|---|---|---|---|---|
| `temperature_unit` | `"F"` | scheduler startup (env override or HA detect) | scheduler `_active_unit`; everything reads unit via `scheduler.get_temperature_unit()` (sync), never DB directly | `GET /api/settings`; display-only |
| `unit_change_ack_required` | `"0"` | scheduler per-tick `_check_unit_change` | `GET /api/settings` | `UnitChangeBanner`; dismiss `POST /api/settings/ack-unit-change` |
| `unit_change_acked_unit` | `""` | `ack_unit_change()` — records which HA unit was dismissed so the banner doesn't re-raise (#288) | scheduler | none (bookkeeping) |
| `system_enabled` | `"1"` | `POST /api/system/enabled` | engine gate | master toggle in `App.tsx` header |
| `developer_mode` | `"0"` | `POST /api/system/dev-mode` | scheduler + `ha.dev_mode` (suppresses real HA writes) | App header + `DevMode.tsx` page |
| `mcp_enabled` | `"0"` | `POST /api/system/mcp` | MCP ASGI app per-request (returns 503 when off) | App header toggle |
| `vacation_mode_enabled` | `"0"` | `POST/DELETE /api/settings/vacation-mode` | scheduler + engine | `VacationModeModal` / `VacationModeBanner` |
| `vacation_mode_return_at` | `""` | same | scheduler `_check_vacation_expiry` (auto-disables past return time) | same; `return_at` required, must be future ISO-8601 |
| `outside_temperature_entity_id` | `""` | `PUT /api/settings/outside-temp-entity` (validates entity exists in HA and is numeric) | cooling lockout (#209), pre-cool (#248), metrics | `OutsideTempPicker` on Thermostats page |
| `sensor_stale_after_min` | `30.0` (`SENSOR_STALE_AFTER_MIN`, `engine/cycle_engine.py:55`) | `PUT /api/settings/sensor-staleness` — guard **1–1440 min** | engine staleness guard (#211), `/api/sensor-health` | Thermostats page |
| `event_log_retention_days` | `"7"` | `POST /api/settings/log-retention` — guard `max(1, int(v))` | log trim job | Logs page |
| `cycle_log_retention_days` | `"30"` | same | same | Logs page |
| `migration_holdover_timestamps_utc_v1` | — | one-shot migration sentinel (#65) | `db.init_db` | none |
| `migration_short_cycle_defaults_v1` | — | one-shot sentinel (#208 backfill: existing thermostats get runtime=10/off=5 min) | `db.init_db` | none |

The scheduler caches `system_enabled` / `developer_mode` / `mcp_enabled` /
vacation state in memory at startup and on `reload_from_db()`; routes read the
cached getters, never the DB.

## Layer 4 — `ThermostatConfig` fields (`smart_vent/backend/models.py`, PK `thermostat_entity_id`)

Written by `POST /api/thermostats` and `PUT /api/thermostats/{entity_id}`;
docs: `docs/thermostat-settings.md`, `docs/safety.md`. UI: Thermostats page
(`SAFETY_FIELDS` loop in `frontend/src/pages/Thermostats.tsx`, `kind:
"absolute_temp" | "delta_temp" | "other"` drives the unit-label suffix).
Numeric safety fields are bounds-checked by `_THERMO_NUMERIC_BOUNDS` in
`routes.py` (#295) — non-numeric or negative values would crash/invert engine
tick arithmetic.

| Field | Default | Unit/kind | Guard (server-side) | Status |
|---|---|---|---|---|
| `name` | `""` | — | — | production |
| `default_temp` | `None` | °F absolute, nullable (fallback presence target) | 40–90 °F post-conversion; null clears | production |
| `min_setpoint` / `max_setpoint` | 60.0 / 85.0 | °F absolute | **40–100 °F** post-conversion; `min < max` | production; hard clamp on every setpoint command |
| `deadband` | 0.5 | °F **delta** | ≥ 0 | production |
| `overshoot_delta` | 2.0 | °F **delta** | ≥ 0 | production |
| `max_vent_closed_min` | 0 (off) | minutes | ≥ 0 | production safety valve |
| `cycle_timeout_hours` | 3.0 | hours | **> 0** (strict; UI input min=0.5) | production safety valve |
| `reconciliation_interval_min` | 0 (off) | minutes | ≥ 0; docs say "should not exceed cycle_timeout×60" but that upper bound is **advisory only — not enforced in code** (engine just skips when ≤0) | production |
| `total_vents_count` | `None` | count (smart + passive registers) | positive int; **required at registration** (#213); null → transitional "≥1 open" fallback + UI banner | production |
| `has_bypass_damper` | `False` | bool | — | production; True disables the airflow floor |
| `min_open_vents_fraction` | 0.333 | fraction | 0 < v ≤ 1 | production; airflow floor = `ceil(total × fraction)` |
| `vacation_hvac_mode` | `"single"` | enum | `"range"` or `"single"` | production |
| `min_cycle_runtime_min` | 0 (off; recommended 10) | minutes | ≥ 0 | production, short-cycle protection (#208) |
| `min_cycle_offtime_min` | 0 (off; recommended 5) | minutes | ≥ 0 | production (#208) |
| `cooling_lockout_below_f` | `None` (off) | °F absolute, nullable | converted via `_to_f`; needs the outside sensor (Layer 3) | production (#209) |
| `overflow_during_min_runtime` | `True` | bool | — | production (#237); auto-off in vacation mode |
| `unavailable_abort_after_min` | 5 | minutes | non-negative int; 0 = never abort (not recommended) | production (#267) |

## Layer 5 — Per-room settings (`Room` in models.py; Rooms page modal)

| Field | Default | Unit/kind | Guard | Notes |
|---|---|---|---|---|
| `name`, `thermostat_entity_id` | required | — | — | |
| `include_thermostat_sensor` | `False` | bool | — | Count the thermostat's own temp in the room average |
| `system_wide_temp` | `None` | °F absolute, nullable | 40–90 °F post-conversion | The room-level **presence target**; falls back to thermostat `default_temp` (docs/presence.md) |
| `presence_holdover_hours` | 2.0 | hours | 0–8760; **0 disables presence activation for the room** | |
| `temp_offset` | 0.0 | °F **delta** | −20 to +20 °F post-conversion | Post-vent-close drift compensation (#86) |
| `deadband_override` | `None` (inherit thermostat deadband) | °F **delta**, nullable | 0–10 °F post-conversion; null clears (#277) | Affects only this room's start/join-cycle vote |
| `ambient_suppression_enabled` | `False` | bool | — | Pre-cool/pre-heat (#248), `docs/precool-presence.md`; inert without outside sensor |
| `ambient_suppression_mode` | `"any_presence"` | enum | `any_presence` \| `off_schedule_only` | |
| `ambient_suppression_min_differential` | 5.0 | °F **delta** | ≥ 0 | Outside must be this far past target to coast |
| `ambient_suppression_deadband` | 2.0 | °F **delta** | ≥ thermostat deadband **only when feature enabled** | Widened coasting band; engine clamps with `max()` anyway |
| `ambient_suppression_off_schedule_window_min` | 60 | minutes | non-negative int | Only for `off_schedule_only` mode |
| `notes` | `""` | — | — | |

Sub-entities: sensors (`sensor.*`), vents (`cover.*` with per-vent
`control_method` ∈ `open_close | set_position | set_tilt_position | toggle`),
presence sensors (`binary_sensor.*`). A room may have **zero vents** —
monitor-only rooms are supported by design (`docs/rooms-and-zones.md`: "Zero
or more vents").

## Layer 6 — Schedules and overrides

**Schedule** (`Schedule` in models.py; Schedules page; `docs/schedules.md`):
`days_of_week` (0=Mon), local `start_time`/`end_time` (overnight blocks
allowed), `target_temp` (°F absolute, guard 40–90 °F post-conversion), plus
lifecycle (#359): `enabled` (default True — False "parks" the block, sweeps
never delete) and `expires_at` (naive **local** wall-clock datetime, null =
never; matches `<input type="datetime-local">`). Rules verified in
`routes.py`: overlap is rejected only between *enabled* blocks; an enabled
block cannot be created already-expired; the scheduler's
`schedule_expiry_sweep` job auto-disables expired blocks but lets a
currently-running block finish first. `POST …/schedules/{id}/copy` clones
days/times/target to other rooms — copies are enabled+never-expiring, and a
conflicting copy is created **disabled** with status
`created_disabled_conflict`.

**Room override** (`POST /api/rooms/{room_id}/override`): `target_temp`
(40–90 °F post-conversion) + `duration_hours` (default 2.0, guard 0–8760).
Priority: override > schedule > presence holdover.

## The temperature-field registry (3-file lockstep)

`TEMPERATURE_FIELDS` in `routes.py` (~line 243) is the Python source of truth
for every body key converted at the write boundary. Kinds: `absolute`,
`absolute_nullable`, `delta`, `delta_nullable`. Current entries: `default_temp`,
`min_setpoint`, `max_setpoint`, `deadband`, `overshoot_delta`,
`cooling_lockout_below_f`, `system_wide_temp`, `temp_offset`,
`deadband_override`, `ambient_suppression_min_differential`,
`ambient_suppression_deadband`, `target_temp`. Mirrored in
`e2e/tests/temperature-fields.ts`; parity + `// @covers:` tags enforced by
`smart_vent/backend/tests/test_temperature_field_parity.py`.

---

## ADD-A-KNOB checklists

Before touching anything, load `plenum-change-control` for gating/evidence.
Remember the CLAUDE.md 100% rule: **every backend tunable gets a UI control**
in the same change.

### A. New `config.yaml` option
1. Add to `options:` AND `schema:` in `smart_vent/config.yaml`.
2. Add a `get_config '<key>' '<default>'` read + `export <UPPER_KEY>=…` in `smart_vent/run.sh` (the parity test greps for `bashio::config '<key>'` usage — `test_addon_config.py` fails otherwise).
3. Read the env var in the backend (`os.environ.get`) with the same default.
4. Verify: `cd smart_vent && python -m pytest backend/tests/test_addon_config.py -q`.

### B. New temperature field on a write boundary
(Gates and reviewer evidence: `plenum-change-control` §2.1; parity-rule
mechanics and failure texts: `plenum-validation-and-qa` §5. This checklist is
the catalog-side companion, not the owner.)
1. Backend: convert with `_to_f` (absolute) or `_delta_to_f` (delta) from `backend/units.py`; validate the **°F value** after conversion (40–90 °F for user targets, per `.jules/sentinel.md`); pick nullable semantics deliberately.
2. Register in `TEMPERATURE_FIELDS` in `routes.py` with the right kind — a wrong absolute/delta kind silently corrupts data.
3. Register in `e2e/tests/temperature-fields.ts` (`field`, `kind`, `ui`, `endpoints`).
4. If `ui: true`: add a round-trip test in `e2e/tests/temperature-units.spec.ts` tagged `// @covers: <field>`.
5. Frontend form: init via `toDisplay`/`toDisplayDelta`, submit the **raw display value** — never `toStorage` on outgoing payloads (#231; contract owned by `plenum-architecture-contract`).
6. Tests: `TestToF`-style unit test + a Celsius-mode integration test (set `client.app["scheduler"]._active_unit = "C"`, restore in `finally`).
7. UI change ⇒ regenerate both `-Fahrenheit-` and `-Celsius-` E2E goldens (see CLAUDE.md pitfall 8).
8. Verify: `cd smart_vent && python -m pytest backend/tests/test_temperature_field_parity.py -q`.

### C. New `system_settings` flag (mirror the `mcp_enabled` pattern — verified in `scheduler.py:256-267`)
1. Scheduler: private cached field (`self._flag`), loaded in startup **and** `reload_from_db()`; sync getter `get_flag()`; async `set_flag()` that (a) updates the cache, (b) `db.set_system_setting(...)` as `"1"/"0"`, (c) logs, (d) broadcasts a WS event `flag_changed` via `self._broadcast` if set. If the flag affects HVAC behavior, also call `_reset_and_reevaluate(...)` (see `set_dev_mode`/`set_vacation_mode`).
2. Routes: GET exposure (piggyback `/api/system/status` or `/api/settings`) + a POST setter that calls the scheduler setter and `emit(...)`s an event-log entry. Every route needs `@docs` + `@response_schema` (enforced by `test_api_spec_enforcement.py`).
3. Frontend: `api.ts` functions, a UI control (App header toggle for system-level flags), and a listener for the WS `*_changed` event to keep the UI live (see `mcp_enabled` handling in `App.tsx`).
4. Tests: integration test toggling via the API and asserting persistence + broadcast.

---

## Provenance and maintenance

Facts date-stamped 2026-07, v0.22.1. Re-verification one-liners (run from repo root):

- Layer 1: `cat smart_vent/config.yaml` ; `grep -n "get_config\|export" smart_vent/run.sh`
- Layer 2: `grep -rn "os.environ\|os.getenv" smart_vent/backend/ --include="*.py" | grep -v tests`
- Layer 3 keys: `grep -rhoE '(get|set)_system_setting\([^,]+, "[a-z_]+"' smart_vent/backend/ --include="*.py" | grep -oE '"[a-z_]+"' | sort -u`
- Layer 3 defaults: `grep -n "SENSOR_STALE_AFTER_MIN" smart_vent/backend/engine/cycle_engine.py` ; `grep -n "retention_days" smart_vent/backend/api/routes.py`
- Layer 4/5/6 fields+defaults: `sed -n '35,230p' smart_vent/backend/models.py`
- Guards: `grep -n "_temp_range_error\|_THERMO_NUMERIC_BOUNDS\|40 <=\|8760\|1440" smart_vent/backend/api/routes.py`
- Registry: `sed -n '243,263p' smart_vent/backend/api/routes.py` ; `cat e2e/tests/temperature-fields.ts`
- mcp_enabled pattern: `sed -n '237,290p' smart_vent/backend/scheduler.py`
- Parity tests: `cd smart_vent && python -m pytest backend/tests/test_addon_config.py backend/tests/test_temperature_field_parity.py -q` (needs `pip install ".[dev]"`; not executed while authoring this skill — deps not verified installed)

Volatile line numbers cited (routes.py ~243, scheduler.py ~256, cycle_engine.py:55, db.py ~2259) will drift; the greps above re-locate them.
