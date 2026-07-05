---
name: plenum-architecture-contract
description: >-
  The load-bearing design decisions of Plenum (smart-thermostat-with-vents),
  the invariants that must hold, WHY each exists, and the known-weak points.
  Load when you need the canonical temperature-unit contract (°F storage,
  single write boundary, #231), the component map with real file paths, the
  cycle-engine invariants (mode lock, in-place trigger updates, 60s tick), the
  MCP loopback design, or a "before you change X, understand Y" dependency
  check. Also load when CLAUDE.md prose seems to disagree with the repo — this
  skill flags the known drift.
---

# Plenum architecture contract

The design decisions this system stands on, stated as invariants with their
rationale. Every claim below was verified against the repo at v0.22.1
(2026-07-04). Where `CLAUDE.md` or `DESIGN.md` prose has drifted behind the
code, the drift is called out — **the repo files win**.

**When NOT to use this skill:**
- Domain theory (why short-cycling kills compressors, what a deadband is, HA
  entity semantics) → `hvac-zoning-reference`.
- The blow-by-blow story of a past incident → `plenum-failure-archaeology`.
- Which CI gates a change must clear, PR/release mechanics → `plenum-change-control`.
- Diagnosing a live symptom → `plenum-debugging-playbook`.
- Adding/locating a config knob → `plenum-config-and-flags`.

Jargon (once): *write boundary* — the POST/PUT/PATCH handlers in
`smart_vent/backend/api/routes.py`, the only place display-unit input becomes
storage °F. *Delta* — a temperature difference (deadband, offset): °C↔°F scales
by 9/5 with **no** 32 offset. *Ingress* — Home Assistant's authenticated
reverse proxy that serves the add-on UI. *Zone* — one thermostat plus its rooms.

---

## 1. Component map (verified file paths and responsibilities)

All backend paths are under `smart_vent/backend/`.

```
HA Core ◄─ WebSocket + REST ─► ha_client.py ◄── engine/ ◄── scheduler.py ◄── api/routes.py ◄── frontend / MCP
                                                    │              │                │
                                                    └──────────────┴── db.py ── app.db (SQLite, DATA_DIR)
```

| File | Responsibility (verified) |
|---|---|
| `main.py` | Entry point. Binds aiohttp on `PORT` (default **8099**), starts the HA client task, the `Scheduler`, and the HTTP MCP server (uvicorn) on `MCP_PORT` (default **9099**). One-shot `flair.db → app.db` rename (#89). Security-headers middleware on every response. |
| `ha_client.py` | Raw HA WebSocket client + state cache. Normalises **sensor** readings to °F via per-entity `unit_of_measurement` (`get_numeric_state`, line ~172), resolves HA's system unit into `ha_temp_unit` from `/api/config` (#281), and converts stored-°F setpoints back to HA-native units on write via `_setpoint_to_native` (#280). Dev mode intercepts all writes here and logs `[DEV] Would …` instead. |
| `scheduler.py` | Orchestrator. Owns `_engines: dict[thermostat_id → CycleEngine]`, the 60s APScheduler `main_tick`, log purge (24h), schedule-expiry sweep (60s, #359), daily/monthly metrics rollups (#85). Owns `_active_unit` and the `system_enabled` / `developer_mode` flags (persisted in `system_settings`). |
| `engine/cycle_engine.py` | One instance **per thermostat**. The `IDLE → RUNNING → TERMINATING → IDLE` state machine; all safety guards (short-cycle lockout, cooling lockout, staleness filter, cycle timeout, airflow floor, #367 envelope enforcement) live in its tick. ~3600 lines. |
| `engine/room_manager.py` | Resolves each room's active trigger: override > schedule > presence holdover. |
| `engine/vent_controller.py` | Issues cover open/close commands per the room-vent `control_method`. |
| `api/routes.py` | All REST endpoints. **The write boundary**: every temperature field is converted via the `units.py` helpers here, then bounds-checked on the °F value. Also `TEMPERATURE_FIELDS` (Python side of the parity system). |
| `api/ws_handler.py` | Thin WebSocket broadcaster (`WSManager`). Event payloads are built in `scheduler.py` / the engine — temperatures in them are raw °F. |
| `units.py` | The four converters: `to_f` / `delta_to_f` / `from_f` / `from_f_delta`. Single source of truth shared by routes.py, ha_client.py, and the engine's HA-ingest helper. |
| `db.py` | aiosqlite layer. `SCHEMA` script (WAL mode, FK on) + append-only `_MIGRATIONS` list of `ALTER TABLE` statements + two one-shot data migrations gated by `system_settings` sentinels. |
| `tz.py` | Single source of local-time truth: `TZ` env (from the `timezone` add-on option via `run.sh`). Schedule matching and metrics bucketing use it; **storage timestamps are UTC**. |
| `mcp_http.py` / `mcp_openapi.py` | HTTP (Streamable) MCP server on port 9099; tools generated from the OpenAPI spec, each call dispatched over **loopback HTTP** to the REST API. |
| `frontend/src/contexts.ts` | `buildUnitContext(unit)` — the frontend display-conversion layer (`toDisplay`, `toDisplayDelta`, `fmtTemp`). |

`DESIGN.md` is the **original** design doc and is stale in places (it still
describes a stdio/SSE MCP server, `min_open_vents` as a count, a 30s monitor
tick). Treat it as historical intent; this skill and the code are current.

---

## 2. The temperature-unit contract (canonical statement — owned here)

**Invariant: every temperature at rest or in flight inside the system is °F.**
SQLite rows, engine arithmetic, scheduler state, WebSocket event payloads, API
GET responses — all °F, always (Issue #123). Exactly **three** places convert,
and nothing else may:

1. **Backend write boundary** (`api/routes.py`): display-unit input → °F via
   `to_f` / `delta_to_f` (imported from `units.py` as `_to_f` / `_delta_to_f`),
   using the active unit from `scheduler.get_temperature_unit()`. Bounds
   (40–90 °F for targets, 40–100 °F for setpoints, 0–10 °F for
   deadband_override) are checked **after** conversion, on the °F value —
   checking the raw input is meaningless when the unit varies
   (`.jules/sentinel.md`, 2026-05-05).
2. **Frontend display layer** (`contexts.ts`): °F → display via `toDisplay` /
   `toDisplayDelta` / `fmtTemp`, for rendering and form **initialisation
   only**. Forms hold display-unit values and submit them raw.
3. **HA I/O boundary** (`ha_client.py` + `_climate_temp_to_f` in
   `cycle_engine.py`): HA-native values ↔ °F. Sensor entities carry a
   per-entity `unit_of_measurement`; `°C` readings are normalised through the
   shared `to_f` (#251). Climate entities report in HA's *system* unit
   (`ha_temp_unit`, resolved from `/api/config` — #281 fixed the detection),
   so climate reads pass through `_climate_temp_to_f` and setpoint writes
   through `_setpoint_to_native` (#280 — sending raw °F to a metric HA would
   be interpreted as °C and drive the HVAC wildly wrong).

**The frontend NEVER calls `toStorage` / `toStorageDelta` on an outgoing
payload.** That was **#231**: the Thermostats form converted 16 °C → 60.8 °F
via `toStorage`, then the backend's `_to_f` converted again → 141.44 °F
stored. Each side's unit tests passed because each asserted a different
contract. The conversion belongs to exactly one side — the backend. The
`toStorage` helpers still exist in `contexts.ts` but are reserved for local
validation-bound math only.

**Absolute vs delta is a type distinction, not a detail.** Absolute uses
`(f−32)×5/9`; a delta uses only `×5/9`. Feeding a delta (deadband, temp_offset,
overshoot_delta) through the absolute converter renders a 2 °F deadband as a
negative °C number (#291 was exactly this in a chart subtitle). `units.py`
keeps all four converters side by side to make the axis visible.

**The parity system** keeps the contract enforced: every write-boundary
temperature field is registered in `TEMPERATURE_FIELDS` in `routes.py`
(field → kind: `absolute` / `absolute_nullable` / `delta` / `delta_nullable`;
11 fields as of v0.22.1) AND in `e2e/tests/temperature-fields.ts`, with a
`// @covers:` round-trip test in `e2e/tests/temperature-units.spec.ts`.
`backend/tests/test_temperature_field_parity.py` fails CI on any drift. The
add-a-field procedure and gates are owned by `plenum-change-control`.

**History note:** older CLAUDE.md copies placed `_to_f`/`_delta_to_f`/`_from_f`
in `backend/api/routes.py`; CLAUDE.md was corrected 2026-07-05 (PR #388). They
live in `backend/units.py` and are imported into routes.py under those aliases
(routes.py lines 35–38). Same semantics; trust `units.py`.

---

## 3. Engine and scheduler invariants

| Invariant | Why | Where verified |
|---|---|---|
| **One `CycleEngine` per thermostat; 60s tick.** The scheduler's `_sync_engines` creates/removes engines as thermostats appear in room configs; `main_tick` runs every 60s (`max_instances=1, coalesce=True`) and also fires on HA state changes. Each engine serialises its work behind an internal `asyncio.Lock`. | Zones are independent failure domains; one thermostat's outage must not stall another's control loop (#286 hardened the per-engine exception isolation). | `scheduler.py` lines ~53, 130–137, 470–481; `cycle_engine.py` `tick()` line 176. |
| **Mode is locked at cycle start** (`_cycle_mode`, cycle_engine line 100). Monitoring, at-target checks, and the setpoint all use the locked mode, not live `hvac_action`. | During HVAC idle phases the naively inferred mode can flip tick-to-tick → heat/cool oscillation. A room that now needs the *opposite* direction is dropped by the mode filter; if no compatible rooms remain the cycle ends cleanly and a new one may start opposite on the next tick. | `docs/cycle-engine.md` §"Why the mode is locked"; `cycle_engine.py` lines ~404, 546, 566. |
| **Mid-cycle trigger changes are applied in place, never by teardown** (#215). A changed source/target updates the running cycle and re-derives the setpoint; the cycle log stays open with a `trigger updated in place` setpoint-history entry. | The old teardown was itself a compressor stop/start — and with the #208/#214 off-time lockout enabled, the rebuild was then *blocked* for the lockout window, leaving a room unconditioned (back-to-back schedule blocks at 08:00 caused a full furnace stop→start for a target that only moved up). | `cycle_engine.py` lines ~886–911, 972; `docs/safety.md` §"In-place cycle updates". |
| **The engine never converts display units.** It receives °F from the DB and commands °F; `ha_client.py` translates to HA-native at the wire. The only conversion helpers it touches are the HA-ingest normalisers (`_climate_temp_to_f`, `to_f` on °C sensors). | Conversion in two layers is how #231 happened; the engine staying unit-blind keeps the write boundary the single converter. | `cycle_engine.py` `_climate_temp_to_f` docstring (line 3589). |
| **The scheduler owns `_active_unit`.** Resolution order: `TEMPERATURE_UNIT` env override → HA `/api/config` on startup → last-known `system_settings.temperature_unit`. Engine and routes read it via `scheduler.get_temperature_unit()`, never from the DB directly. | One synchronous, cached source; a mid-request DB read could race a unit change. Unit changes set `unit_change_ack_required` and surface a banner rather than silently re-labelling data. | `scheduler.py` lines ~61, 102–105, 293–295, 366–374. |
| **`EntityState.numeric` is always °F.** `POST /api/ha/states` converts `°C` entity values to °F (and rewrites the unit label) before returning. | The frontend must apply exactly one conversion (`fmtTemp`); if raw HA values leaked through, °C sensors would double-convert on display. | `routes.py` lines ~1378–1387. |
| **MCP dispatches via loopback HTTP — one write boundary** (#372, shipped 0.22.0/0.22.1). Tools are generated from the OpenAPI spec; every call becomes `session.request(...)` against `http://127.0.0.1:8099`, hitting the same handlers, validation, unit conversion, and logging as the UI. The MCP server is a separate ASGI (Starlette/uvicorn) stack on port 9099 because aiohttp is not ASGI; `/mcp` returns 503 unless the `mcp_enabled` toggle is on (off by default). | "No duplicated business logic, no #231 double-conversion" — the module docstring says it outright. A second code path into the DB would eventually drift from the write boundary. | `mcp_http.py` (whole file, esp. docstring and `dispatch_tool`); `main.py` `_start_mcp_server`; `docs/mcp.md`. |
| **System-off means no HA writes.** With `system_enabled` false (and dev mode off), the engine tick returns before any control logic; the sole exception is a one-time abort of a cycle that was in flight when the system was disabled (which restores setpoint/vents). Idle-state vent reconciliation is also gated on enabled. Dev mode goes further: `ha_client` intercepts every write and logs `[DEV] Would …`. | An operator disabling the system must be able to trust that Plenum stops touching their equipment, without leaving a half-run cycle holding the thermostat at an overshot setpoint. | `cycle_engine.py` lines 315–322, 376; `ha_client.py` dev_mode branches (lines ~287, 317, 335). |
| **SQLite, single file `app.db` in `DATA_DIR`, WAL mode; `system_settings` is a key-value table** (flags: `temperature_unit`, `unit_change_ack_required`, `system_enabled`, `developer_mode`, `mcp_enabled`, `sensor_stale_after_min`, migration sentinels, …). No settings table, no ORM. | Zero-dependency persistence fitting the add-on model; KV avoids a schema migration per flag. `DATA_DIR` is `/config` in the add-on (config.yaml `environment:`), `/data` by default elsewhere. | `db.py` SCHEMA (line 38, `system_settings` at 189); `main.py` lines 42–43. |
| **WebSocket events carry raw °F.** Zone-status payloads are built from engine state (already °F); the frontend converts for display. | Same single-conversion rule as REST GETs. | `scheduler.py` `_zone_status_dict`; `api/ws_handler.py` is payload-agnostic. |
| **All schedule times are local wall-clock in the configured timezone; storage timestamps are UTC.** `tz.py` is the only legitimate source of "local now" — it reads `TZ` (from the `timezone` add-on option via `run.sh`). Schedule `start_time`/`end_time`/`expires_at` are naive local; cycle/event timestamps are `datetime.now(UTC)`. | Mixing the two produced real bugs: #65 (holdovers stored local, compared as UTC), #294 (vacation modal min computed in UTC), #301 (naive-UTC rendered as local). | `tz.py` module docstring; `db.py` schedules table comment (line 89) and `_migrate_holdover_timestamps_to_utc`. |
| **Safety monitoring must survive degraded states.** The thermostat-availability check runs *before* the enabled/vacation guards; a sustained outage with a cycle in flight aborts it after `unavailable_abort_after_min` (default 5). The #367 envelope check runs even with zero active rooms. | #267: an unavailable thermostat used to suspend *all* per-tick safety monitors while the physical HVAC kept running at the last commanded setpoint with vents closed. #367: with no active rooms, nothing enforced `max_setpoint`. | `cycle_engine.py` lines 263–300 (with the #267 comment), 358–381. |

---

## 4. Known-weak points (stated plainly)

| Weakness | Status | Detail |
|---|---|---|
| **No authentication on the direct ports** | **Open — #373**, the biggest open design problem | Ingress access is authenticated by HA, but `config.yaml` lets users publish 8099 (web UI) and 9099 (MCP) directly, and neither has any auth. The MCP endpoint exposes the **full write surface**; its only guards are "off by default" and "port unpublished by default". Do not add features that assume a trusted network without checking #373's direction first. |
| **Ad-hoc DB migrations** | **Open — #21** | `_MIGRATIONS` is a list of `ALTER TABLE` strings run under `try/except: pass` ("column already exists") on every startup — no version table, no ordering guarantees, no down-migrations, and a genuinely failing migration is silently swallowed. Two one-shot data migrations use `system_settings` sentinels instead. Unwritten-but-observed rule (inferred from history): schema changes are additive-only, never lossy. |
| **Container runs as root** | **Open — #23** (docker hardening; see also #173 base image) | No `USER` directive; the add-on process has root in-container. |
| **Heat pumps unsupported** | Deliberate scope cut | Stated in `docs/safety.md`. The cooling lockout has no heating equivalent because a conventional furnace is assumed; reversing-valve/defrost semantics would invalidate several guards. Don't "fix" cold-weather heating behaviour by analogy with the cooling lockout. |
| **OpenAPI decorators are documentation-only** | Verified current at v0.22.1 | `@docs` / `@request_schema` / `@response_schema` (`api/openapi.py`, #188) install **no validation middleware** — handlers parse `await request.json()` themselves. #295 (closed) did **not** add schema-driven validation; it added targeted per-field validators in routes.py: `_THERMO_NUMERIC_BOUNDS` + `_validate_thermostat_numeric` for the safety-critical numeric fields, `_validate_deadband_override`, `_validate_ambient_suppression`. Any *new* body field is unvalidated unless you validate it by hand — the schema will happily document a constraint nobody enforces. |
| **`DESIGN.md` is stale** | Historical doc | See §1. `docs/` pages are the docs of record for shipped behaviour. |
| **CLAUDE.md drift** | Trust the repo files; CLAUDE.md corrected 2026-07-05 (PR #388) | The 2026-07-04 audit found and fixed: (a) unit helpers live in `backend/units.py`, not routes.py (§2); (b) the visual-regression E2E suite lives in `.github/workflows/container-ci.yml` — there is **no** `e2e.yml` anymore; (c) coverage thresholds ratcheted to backend `fail_under = 92.5` (`pyproject.toml`), frontend 90/85/72/87 (`vite.config.ts`). If CLAUDE.md and the repo disagree again, the repo wins — re-run the Provenance checks. |

---

## 5. Before you change X, understand Y

| Before you change… | …understand | Because |
|---|---|---|
| Any temperature field on a write path | §2 in full + the 3-file parity system (`plenum-change-control` §2.1) | The exact #231 bug class; parity CI fails on any one-sided edit. |
| Conversion math anywhere | Absolute-vs-delta axis (`units.py` docstring; `hvac-zoning-reference` §4) | The wrong axis silently corrupts values (#291). |
| `_do_tick` guard ordering in `cycle_engine.py` | Availability check < enabled < vacation < heat_cool revert < active rooms < safety rooms (#367) — the order **is** the safety model | #267 happened because everything lived below a guard that returned early. Moving a guard can silently suspend the monitors beneath it. |
| Cycle teardown / `_abort_cycle` call sites | In-place update rule (#215) + off-time lockout interaction (#208/#214) | Every new teardown path is a potential short-cycle, and the lockout then blocks the restart. |
| Mode inference or the mode filter | Mode-lock invariant (§3) and opposite-cycle prevention (`docs/safety.md`) | Oscillation is the failure mode the lock exists to prevent. |
| Minimum-runtime hold / overflow logic | `docs/overflow-conditioning.md` tiering + the `in_min_runtime_hold` close-loop gate | #237: without the gate, the close loop re-closes vents the hold just opened (churn); tier-3 headroom exclusion is what makes "cannot create an opposite cycle by construction" true. |
| `ha_client.py` read/write paths | The HA I/O boundary (§2 item 3) | #280/#281: climate entities use HA's *system* unit, sensors use per-entity units — they need different normalisation. |
| Anything reading "now" for schedules/metrics | `tz.py` contract (local wall-clock via `TZ`; storage in UTC) | #65/#294/#301 were all naive-time mixups. |
| `Scheduler` job wiring or engine lifecycle | One-engine-per-thermostat + `_tick_engine` exception isolation (#286) | A pre-tick exception used to silently stop a zone being controlled. |
| REST handlers / adding endpoints | Decorators don't validate (§4); CWE-209 rule (`log.exception` + generic `error(...)`); every knob needs a UI control | All three are standing rules; the MCP surface auto-inherits every endpoint you add, unauthenticated (#373). |
| DB schema | Additive-only `_MIGRATIONS` pattern + sentinel data-migrations (§4, #21) | There is no rollback; a lossy migration bricks user data on upgrade. |
| MCP tool behaviour | Loopback design (§3) — fix the REST handler, not the tool | Tools are generated; there is nothing MCP-side to patch without recreating the dual-path problem. |
| Anything rendered in the UI | Golden screenshots + `<Frozen>` determinism (owned by `plenum-ci-and-release` / CLAUDE.md) | Visual-regression legs in container-ci fail until goldens regenerate in both unit sets. |

---

## Provenance and maintenance

All facts verified 2026-07-04 against v0.22.1 (`smart_vent/config.yaml`).
Volatile facts and how to re-verify:

- **Helper location / aliases**: `grep -n "from ..units import" smart_vent/backend/api/routes.py` (lines 35–38 as of 2026-07).
- **TEMPERATURE_FIELDS count (11)**: `grep -c '":' <(sed -n '/^TEMPERATURE_FIELDS/,/^}/p' smart_vent/backend/api/routes.py)` or read routes.py lines ~243–262.
- **Ports 8099/9099**: `grep -n "PORT" smart_vent/backend/main.py`; `grep -n "tcp" smart_vent/config.yaml`.
- **Tick cadence / jobs**: `grep -n "add_job" -A4 smart_vent/backend/scheduler.py`.
- **Coverage thresholds** (ratchet upward over time): `grep fail_under smart_vent/pyproject.toml`; `grep -A4 "lines:" smart_vent/frontend/vite.config.ts`.
- **Workflows** (e2e.yml is gone): `ls .github/workflows/`.
- **#295 validation state** (re-check if a validation middleware ever lands): `grep -n "_THERMO_NUMERIC_BOUNDS\|request_schema" smart_vent/backend/api/routes.py smart_vent/backend/api/openapi.py`.
- **Open weak-point issues**: #373 (auth), #21 (migrations), #23 (docker hardening) — confirm still open via `gh issue view <n>` before citing as open.
- **`system_settings` keys in live use**: `grep -rn "get_system_setting(" smart_vent/backend --include=*.py -h | grep -o '"[a-z_]*"' | sort -u`.
