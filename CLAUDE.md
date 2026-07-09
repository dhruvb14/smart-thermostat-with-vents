# Plenum — Developer Notes for Claude

## Workflow conventions

- **After every push to a PR**: always update the PR body with a description of what changed, why, and a test plan. Do this every time without being asked.
- **When reviewing a PR and pushing fixes**: push directly to the PR's own branch (not to a separate review branch), so the fixes appear in the PR diff.
- **Never include Claude session links** in commit messages, PR titles, PR bodies, or issue comments.
- **Never mention that code was written by Claude** (or any AI assistant) in commit messages, PR titles, PR bodies, or issue comments.
- **Never leak real-world names from the production system.** Data pulled from the live Plenum install — MCP tool output, event logs, screenshots — can contain personal names (rooms are often named after people). Before quoting it anywhere public (GitHub issue titles/bodies, PR titles/bodies, commit messages, code comments, test fixtures, docs), replace names with a generic description ("an upstairs office"), the room's opaque UUID, or an invented name. This applies even when the user pasted the data themselves. Scrubbing after the fact is painful (GitHub keeps issue/PR edit history and force-pushed commits stay fetchable by SHA), so get it right on first publish.
- **Never leak exception detail into API responses.** In `except` blocks inside route handlers, call `log.exception("…context…")` (or `log.error("…", exc_info=True)`) to preserve the full traceback in server logs, then return a generic user-safe message via `error("…", status=5xx)` — never embed `{exc}` or `str(exc)` in the response body. Exposing raw exception strings is an information-disclosure vulnerability (CWE-209) and was the subject of security alert #4. Pattern to follow:
  ```python
  except Exception:
      log.exception("Failed to <action> for <context>=…")
      return error("Failed to <action>", status=502)
  ```
- **Every backend/API feature must have a UI control.** When you add a new `ThermostatConfig` field, system setting, or any other tunable on the API write boundary, also add a form control + helper text to the matching React page (Thermostats, Rooms, Schedules, Settings, etc.). A feature exposed only in the DB/API and not the UI is an incomplete feature — the user cannot reach it. This is a 100% rule; if you find yourself adding a knob without surfacing it, stop and add the UI before considering the work done.

## Skill library (`.claude/skills/`) — load before working

This repo ships 16 project skills. They hold the deep material (incident
histories, verified runbooks, catalogs) so this file can stay short — when a
task matches a skill, load it instead of re-deriving from source. Quick router:

| Task | Skill |
|---|---|
| Any change: gates, review evidence, non-negotiables + their incidents | `plenum-change-control` |
| Something misbehaves at runtime (wrong temps, stuck cycles, thrashing vents, dead zone, shifted times) | `plenum-debugging-playbook` |
| "Was this bug seen before?" / settled decisions — do not re-fight | `plenum-failure-archaeology` |
| Invariants, °F contract ownership, component map, before-you-change-X | `plenum-architecture-contract` |
| HVAC/zoning domain theory, °F/°C math, HA entity model | `hvac-zoning-reference` |
| Any config axis: options, env vars, system_settings keys, field catalogs, add-a-knob checklists | `plenum-config-and-flags` |
| Fresh-clone setup, install/build traps | `plenum-build-and-env` |
| Running the add-on/Docker/local, data paths, backup/restore, MCP attach | `plenum-run-and-operate` |
| Measuring behavior: DB queries, metrics semantics, ready-made scripts | `plenum-diagnostics-and-tooling` |
| Running/writing the test suites, parity/enforcement failures, Celsius patterns, goldens | `plenum-validation-and-qa` |
| CI red, golden-bot pushes, workflows, releases | `plenum-ci-and-release` |
| Editing docs/README/CHANGELOG, house style, claims discipline | `plenum-docs-and-writing` |
| Auth work (#373) | `plenum-auth-campaign` |
| Proving a fix correct (conversion algebra, state tables, off-by-ones) | `plenum-proof-and-analysis-toolkit` |
| Research directions / experiments on cycle data | `plenum-research-frontier` + `plenum-research-methodology` |

Where a skill and this file disagree, the repo source wins — then fix whichever
document drifted (skills carry re-verification commands in their Provenance
sections for exactly this).

## What this repo is

A Home Assistant add-on that replaces the Flair cloud app. It controls HA `cover` entities (vents) while reading temperature from HA `sensor` entities and commanding HA `climate` entities (thermostats). The UI is a React SPA served via HA ingress.

```
smart_vent/
├── backend/          # Python 3.12, aiohttp, asyncio
│   ├── api/routes.py # All REST endpoints (~2500 lines)
│   ├── scheduler.py  # Orchestrator: unit detection, DB init, APScheduler jobs
│   ├── engine/       # cycle_engine.py, room_manager.py, vent_controller.py
│   ├── ha_client.py  # Raw HA WebSocket API client
│   ├── db.py         # aiosqlite helpers (~2400 lines)
│   ├── models.py     # Dataclasses (Room, Schedule, CycleLog, …)
│   └── tests/        # pytest suite
├── frontend/src/
│   ├── pages/        # Dashboard, Rooms, Schedules, Thermostats,
│   │                 # Logs, Metrics, DevMode
│   ├── components/   # UnitChangeBanner, EntityPicker, charts/MetricsCharts
│   ├── contexts.ts   # SystemContext, UnitContext, DevModeContext
│   ├── api.ts        # All fetch() calls to /api/*
│   └── App.tsx       # Router, context providers, UnitChangeBanner
├── config.yaml       # HA add-on manifest (options schema)
├── run.sh            # Add-on entry point — reads bashio config, exports env vars
└── pyproject.toml    # Build, ruff, pytest, coverage config
```

---

## Temperature unit system (Issue #123)

### Core invariant
**All temperatures are stored in °F** in SQLite and passed through the engine, scheduler, and WebSocket events as °F. **Only the backend write boundary and the frontend display layer touch unit conversion** — no other code path converts temperatures.

```
                user types "16" (°C)
                       │
                       ▼
   ┌──────────────────────────────────────────┐
   │  Frontend form state — holds DISPLAY     │
   │  units (°C or °F as the user types).     │
   │  Sends the raw display value as-is.      │
   └──────────────────────────────────────────┘
                       │
                       │  POST { min_setpoint: 16 }
                       ▼
   ┌──────────────────────────────────────────┐
   │  Backend API write boundary —             │
   │  `_to_f(16, "C")` → 60.8°F before storage.│
   └──────────────────────────────────────────┘
                       │
                       ▼
                  DB stores 60.8°F
                       │
                       │  GET returns 60.8 (always °F)
                       ▼
   ┌──────────────────────────────────────────┐
   │  Frontend display — `toDisplay(60.8)` →  │
   │  16°C for rendering; form-init only.     │
   └──────────────────────────────────────────┘
```

**The frontend MUST NOT call `toStorage` / `toStorageDelta` on outgoing payloads.** That was the #231 double-conversion bug: the frontend converted °C → °F via `toStorage`, *and* the backend's `_to_f` converted again, so 16 °C arrived at the DB as 141.44 °F. The conversion belongs to exactly one side — and per the contract above, that side is the backend.

### Backend helpers (`backend/units.py`, imported by `backend/api/routes.py`)

```python
# Defined in backend/units.py; routes.py imports them as _-prefixed aliases:
to_f(value, unit)         # _to_f — absolute temp, display unit → °F (2dp)
delta_to_f(value, unit)   # _delta_to_f — delta (deadband, offset), display unit → °F (2dp)
from_f(value, unit)       # _from_f — °F → display unit (1dp), None → ""  [CSV export only]
from_f_delta(value, unit) # _from_f_delta — delta °F → display unit  [CSV export only]
```

**Every POST/PUT/PATCH endpoint** that accepts a temperature field must call one of these.
Fields that use `_to_f`: `target_temp`, `system_wide_temp`, `default_temp`, `min_setpoint`, `max_setpoint`, `cooling_lockout_below_f`
Fields that use `_delta_to_f`: `temp_offset`, `deadband`, `overshoot_delta`

The active unit comes from `request.app["scheduler"].get_temperature_unit()` (returns `"F"` or `"C"`).

### HA entity state normalisation
`POST /api/ha/states` converts °C sensor values to °F before returning them
(`routes.py` lines ~704-706). So `EntityState.numeric` is **always °F** in the frontend.
This means `avgTemp` on room cards must go through `fmtTemp()`, not just append `unitLabel`.

### Frontend unit context (`contexts.ts`)

```typescript
buildUnitContext(unit)   // pure factory — use this in tests
useUnit()                // hook — reads UnitContext

// UnitContextValue members:
toDisplay(fahrenheit)         // absolute °F → display unit (1dp float)
toDisplayDelta(fahrenheitDelta) // delta °F → display unit (2dp float, NO -32)
fmtTemp(fahrenheit)           // formatted string e.g. "21.1°C"
unitLabel                     // "°F" | "°C"
unit                          // "F" | "C"
isCelsius                     // boolean

// Inverse helpers — defined but NOT used on outgoing payloads. Reserved for
// validation bounds (`toStorage(40)` to compute a °F min) and the rare case
// where display→storage is needed locally. NEVER call these on form values
// you are about to POST/PUT — that re-introduces the #231 double-conversion.
toStorage(displayValue)       // display unit → °F (2dp float)
toStorageDelta(displayDelta)  // display unit delta → °F (2dp float)
```

**Absolute vs delta**: absolute uses `(f-32)*5/9`; delta uses only `f*5/9` (no offset).
This matters for deadband, overshoot_delta, temp_offset.

### Frontend form contract (post-#231)
Forms that submit temperature fields (Thermostats, Rooms, Schedules) store **display-unit** values in their state.
- **On init**: convert °F values returned from the API via `toDisplay` / `toDisplayDelta`.
- **On change**: store the raw `parseFloat(e.target.value)` — no conversion.
- **On submit**: send the display value as-is. The backend's `_to_f` / `_delta_to_f` converts at the write boundary.
- **For display in the same form** (e.g. vacation help text echoing `form.min_setpoint`): the value is already display — render directly, do not re-call `toDisplay`.

Validation bounds that are naturally expressed in °F (e.g. "must be between 40 °F and 90 °F") can use `toDisplay(40)` / `toDisplay(90)` to convert the bound into display units for comparison with the form value. That use of `toDisplay` is fine; it has nothing to do with outgoing payloads.

### SAFETY_FIELDS pattern (Thermostats.tsx)
Each field entry has a `kind: "absolute_temp" | "delta_temp" | "other"`.
The render loop uses `kind` to drive the label suffix (`(${unitLabel})` appended only for temp fields). Form values are already display units, so the render reads them directly — no per-field conversion on the read or write side.

### Unit detection flow
1. `TEMPERATURE_UNIT` env var (from `run.sh` / `config.yaml`) — highest priority
2. HA `/api/config` `unit_system` on startup
3. Last-known value from `system_settings` DB table

`Scheduler._active_unit` holds the resolved unit. `get_temperature_unit()` returns it synchronously.

### Unit change banner
When HA unit changes, `unit_change_ack_required` is set in DB.
`UnitChangeBanner` polls `GET /api/settings` on mount and shows if flag is set.
Dismiss: `POST /api/settings/ack-unit-change`
Restart: `POST /api/restart`

---

## Testing

### Backend
```bash
cd smart_vent && python -m pytest backend/tests/ -v
```
Coverage threshold: **93.9%** (`pyproject.toml` `fail_under = 93.9`). Subset runs
need `--no-cov` or the gate fails the partial run.

**Test patterns:**
- Unit tests for pure helpers: `tests/test_units.py` (TestToF, TestDeltaToF, TestFromF, TestFromFDelta)
- Integration tests use `TestClient` against a full aiohttp app with a temp SQLite DB
- To test Celsius mode in integration tests:
  ```python
  client.app["scheduler"]._active_unit = "C"
  # ... test ...
  client.app["scheduler"]._active_unit = "F"  # restore in finally
  ```

### Frontend
```bash
cd smart_vent/frontend && npx vitest run
npx vitest run --coverage   # also checks thresholds
```
Coverage thresholds (in `vite.config.ts`): lines 90.9, functions 86.9, branches 75.5, statements 88.5.

**Test patterns:**
- Celsius mode: wrap with `<UnitContext.Provider value={buildUnitContext("C")}>`
- System context: wrap with `<SystemContext.Provider value={mockSystem}>`
- Mocks: `vi.mock("../api")` at module level, `vi.mocked(api.fn).mockResolvedValue(...)` per test
- **Temp-write assertion (#231)**: in Celsius mode, assert the POST/PUT body contains the user's **raw display value** (e.g. `min_setpoint: 16`), NOT the pre-converted °F. The backend converts; the frontend must not.

### E2E round-trip (`e2e/tests/temperature-units.spec.ts`)
Matrix-runs against both °F and °C stacks via the `conversion` job in `.github/workflows/container-ci.yml`. Each test edits a temperature field through the UI, reloads, and asserts the field shows exactly the value the user typed. This is the only place the full conversion contract is exercised end-to-end, and would have caught the #231 double-conversion bug — neither side's unit tests did.
- °C run uses `docker-compose.test.celsius.yml` as an override on top of `docker-compose.test.yml` to set `TEMPERATURE_UNIT=C`.
- `PLENUM_TEMP_UNIT` env var tells the spec which unit the stack is in so the assertion values are scaled accordingly.

### E2E visual regression (inside `.github/workflows/container-ci.yml` since #366; originally re-enabled per #182)
A separate suite from the round-trip above: it screenshots every page against committed golden PNGs in `e2e/screenshots/` and fails on any pixel deviation. It is the only test that catches *rendering* regressions (layout, a setpoint showing `158°F` instead of `70°F`, a chart axis flipping units). There is no standalone `e2e.yml` anymore — the suite runs as the `E2E visual regression (F)` / `(C)` jobs in container-ci, with a fan-in `Commit updated goldens` job. Hard-won facts:

- **Determinism via `isCI` / `<Frozen>` (`frontend/src/ci.tsx`).** `isCI = import.meta.env.VITE_APP_VERSION === "CI"`. The screenshot stack builds the image with `config.yaml` `version: CI`, so the bundle bakes `VITE_APP_VERSION=CI`. `<Frozen>{volatile}</Frozen>` renders a frozen placeholder (`—`) under CI and the live value otherwise. Wrap **anything that changes between two renders of the same fixture**: wall-clock strings ("Updated HH:MM:SS"), countdown timers, active-room counts, progress bars, and the engine-driven Dashboard action feed. (The Logs page's event feed and cycle-log table are NOT frozen — they render seeded demo data instead; see the next bullet.) Do **not** freeze static fixture values (live temps, vent positions) — those are stable and freezing them defeats the test. Without this, the update pass and verify pass render differently and goldens are never stable.
- **Charts render real pixels via seeded demo data, not `<Frozen>` (#442).** Accumulating engine data was the third source of nondeterminism, and freezing the whole Metrics data region hid every chart from the visual suite. Instead: the E2E global setup calls `POST /api/dev/seed-demo-metrics` (dev-mode-gated, deterministic, fixed past window 2025-06-01 → 2025-06-07, `demo-` prefixed rows exempt from retention purge), `ci.tsx` pins the page's date range to that window under CI (`CI_METRICS_RANGE` — must match `backend/demo_seed.py`), and every recharts series sets `isAnimationActive={chartAnimationActive}` (also from `ci.tsx`; recharts animations are JS-driven, Playwright's `animations: "disabled"` can't reach them). Live engine cycles are dated "now" and fall outside the pinned window, so they can't perturb the goldens. Playwright pins `timezoneId: "UTC"` + `locale: "en-US"` so localized timestamps (vent timeline) render identically on every runner. Prefer this seeded-fixture pattern over freezing when the volatile thing is *data* rather than *time*. The Logs page uses the same pattern: the seeder also writes demo-flagged `event_log` rows (`"demo": true` in the details JSON — exempt from retention purge and trim), `CI_LOGS_RANGE` pins both Logs tabs' time windows to the demo week, the Live Feed starts paused under CI so websocket pushes can't append between passes, and `logs.spec.ts` expands one event and one cycle so the goldens cover the detail rendering.
- **The room active-status line is pinned via a backend clock override, not `<Frozen>` (#456).** Whether a room reads `via Schedule` vs `Not active` — and the `next … Wed 6:00 PM` label — is computed by the **backend** (`POST /api/rooms/active-status` → `room_manager.get_room_active_status` → `_find_matching_schedule`), so it's a pure function of the backend's "now". `<Frozen>` (frontend) and Playwright's `page.clock` (browser) can't reach it. Instead, `tz.now_utc()` honours `PLENUM_CLOCK_OVERRIDE` (an ISO-8601 instant set **only** in `docker-compose.test.yml`: `2025-06-04T10:00:00-04:00` = Wed 10:00 ET, a weekday inside the seeded demo week). Scope is deliberately narrow — **only the status read path** calls `now_utc()`; `tz.now_local()` and the live engine stay on the real clock (variant "1a"), so pinning the display can't stall cycle timing, holdover expiry, or the schedule-expiry sweep. `TIMEZONE: America/New_York` in the compose makes the UTC→local conversion explicit. At that instant `e2e/global-setup.ts` seeds one room per status permutation: Living Room (schedule active + a later "then" block), Bedroom (upcoming schedule), Office (presence — a continuously-`on` `binary_sensor.office_occupancy` in the HA fixture, armed by the engine's continuous-presence refresh, so `global-setup` polls active-status until `source=presence`), Kitchen (idle). `e2e/tests/fixtures.ts` pins `page.clock.setFixedTime` to the same absolute instant (`14:00Z`) so the browser and backend clocks agree.
- **Keep the `isCI` branch in one place.** All page call-sites use `<Frozen>`; the single `import.meta.env` branch lives only in `ci.tsx`, tested both ways in `ci.test.tsx` (`vi.resetModules()` + `vi.stubEnv("VITE_APP_VERSION","CI")` + dynamic import). Branching inline on `isCI` in each page would tank frontend branch coverage — funnel it through `<Frozen>`.
- **Dual-unit goldens.** container-ci matrixes `unit: [F, C]` so conversion regressions are caught in both directions. Golden filenames encode the unit (`dashboard-Fahrenheit-chromium.png` vs `dashboard-Celsius-chromium.png`) via `playwright.config.ts`'s `PLENUM_TEMP_UNIT`-driven `snapshotPathTemplate`. The °C leg layers `docker-compose.test.celsius.yml`; the matrix varies only the **addon's** display unit.
- **The HA fixture must pin `unit_system: us_customary`** (`e2e/fixtures/ha-config/configuration.yaml`). This was the root cause of the `158°F` bug: a HA YAML config with no `unit_system` defaults to metric/°C, so `generic_thermostat target_temp: 70` is read as 70 °C and the backend's *correct* °C→°F normalisation surfaces it as 158 °F. HA itself is always °F in the fixture; the matrix toggles only Plenum's display unit.
- **Legs run in parallel; the commit is fanned in.** The two legs upload regenerated goldens as artifacts (`goldens-F` / `goldens-C`); only the single `Commit updated goldens` job commits them, so there is no push race (the old `max-parallel: 1` serialization is gone). That job pushes with `HEAD:"$BRANCH"` (the #369/#370 detached-HEAD fix). GITHUB_TOKEN pushes don't re-trigger workflows, so each leg runs its own "verify with updated goldens" pass **in the same job**, and the workflow has `paths-ignore: e2e/screenshots/**` so the bot commit can't loop.
- **Expect golden-bot pushes on any code PR — always fetch/rebase before pushing.** Runner rendering drift can regenerate goldens even when your change is backend-only, so the bot may add a `ci: update E2E golden screenshots` commit to your branch while you work. Never force-push over it. (Docs-only PRs skip the visual legs entirely since #412.)
- **A golden rewrite lands on a GREEN run.** Pass 1 is `continue-on-error`, so a leg whose regenerate+verify succeeds concludes success — the signals are the bot commit and the PR comment `commit-goldens` posts listing every changed PNG (#415). Review those PNGs in the diff like code; do not assume green means "no rendering change".
- **Reuse the prebuilt image; don't rebuild — except on release PRs, which get a second, frozen-UI-only image.** Normal same-repo PRs and forks: the visual legs consume the same image container-ci's `Build (PR validation)` job just produced (pull the `ci-<sha>` tag, or `docker load` the artifact on fork PRs), tagged `plenum-e2e` (the compose default); `validate-release.yml` does the same. That prebuilt image is already `version: CI`, so the frozen UI is baked in. Release PRs are the one exception: the *published* `:<version>` image must keep the real version (for the footer and for HA Supervisor), so it is **not** `isCI`-frozen and would never match a golden — the build job therefore builds a second, throwaway, single-arch image with `version: CI` pinned *after* pushing the real one, and hands it off via the same `plenum-image` artifact/`docker load` path as forks. Smoke test and the round-trip legs still test the real published image; only the visual-regression legs use the frozen one.
- **The round-trip spec is excluded here** (`--grep-invert "Temperature round-trip"`): it mutates shared backend state (creates schedules), so it can't survive two projects (chromium + mobile) or the update→verify double pass on one stack. It's covered by container-ci's conversion job instead.
- **Tall pages need the 30s screenshot budget (`expect.timeout` in playwright.config.ts) — don't shrink it.** `toHaveScreenshot` keeps capturing until two CONSECUTIVE shots match, and the first fullPage capture of a page always differs: the capture's viewport resize settles ~2px of trailing layout (measured 6801px → 6799px on the Thermostats settings panel, with the overlapping pixels byte-identical). Proving stability therefore takes three captures at ~1.7s each (more at mobile 3× DPI), which cannot fit Playwright's default 5s assertion timeout — the tallest pages then fail with "Failed to take two consecutive stable screenshots" on whichever leg's runner is slowest that day. The fixtures.ts auto-fixture additionally pins `scrollbar-gutter: stable` so viewport and capture renders wrap identically.
- **High-DPI mobile amplifies sub-pixel jitter.** The `mobile` project uses `deviceScaleFactor: 3`, so native widgets like `<input type="date">` jitter ~9× more than desktop. `metrics.spec.ts` needs `maxDiffPixels: 800`; the global default is 100. Prefer a per-spec `maxDiffPixels` bump over masking, so the rest of the page is still pixel-checked.

### Temperature field registry (parity-enforced)
Every temperature field on a write boundary is registered in two manifests kept in lockstep:
- **Python**: `TEMPERATURE_FIELDS` dict in `smart_vent/backend/api/routes.py` (`field` → `kind`, where `kind` ∈ `absolute | absolute_nullable | delta | delta_nullable`).
- **TypeScript**: `TEMPERATURE_FIELDS` array in `e2e/tests/temperature-fields.ts` (`field`, `kind`, `ui`, `endpoints`).

Each test in `temperature-units.spec.ts` tags itself with `// @covers: <field>[, <field>...]`.

`smart_vent/backend/tests/test_temperature_field_parity.py` enforces:
1. The Python and TS field sets match (no field on one side only).
2. The `kind` agrees between Python and TS for each shared field (mismatched `_to_f` vs `_delta_to_f` silently corrupts data).
3. Every TS entry with `ui: true` has a `// @covers:` tag in the spec.
4. No `// @covers:` tag references an unknown field.

Adding a temperature field anywhere on a write boundary therefore requires touching all three files. CI fails loudly otherwise — the exact class of bug #231 was.

### Config parity test
`tests/test_addon_config.py` — asserts every key in `config.yaml`'s `options:` block
has a `bashio::config '<key>'` call in `run.sh`. Add new options to **both** files.

---

## CI (`.github/workflows/lint.yml`)

| Job | What it runs |
|---|---|
| Python (ruff) | `pip install ".[dev]"` then `ruff check backend/` + `ruff format --check backend/` |
| Python (pytest) | `pip install ".[dev]"` then pytest with coverage |
| Python (mypy) | `pip install ".[dev]"` then `mypy backend/ --ignore-missing-imports` |
| Frontend (ESLint + Prettier) | `npm ci` then `npm run lint` + `npm run format:check` + `npm run test:coverage` |
| Security (Trivy source scan) | `trivy fs` vulnerability scan of the source tree |

**Container CI (`.github/workflows/container-ci.yml`)** builds the addon image **once** and reuses it for the **Build (PR validation)**, **Docker Smoke Test**, and **Round-trip (F)/(C)** temperature-conversion E2E checks — instead of rebuilding the container ~4× per PR. See #333. `docker-compose.test.yml`'s `plenum` service reads `${PLENUM_IMAGE}` so each E2E leg reuses the prebuilt image rather than rebuilding. The `build` job picks a mode at runtime (#337):
- **Normal same-repo PR** → multi-arch build, push `ghcr.io/<repo>:ci-<pr-sha>` (throwaway); downstream jobs **pull** it.
- **Release PR** (`release/vX.Y.Z` head) → multi-arch build, push the **real** `:<version>` + `:latest`, then Trivy-scan. Smoke test and the round-trip (F)/(C) legs pull the explicit `:<version>` tag (never `:latest`). This is the publish + scan that used to be `docker.yml`'s `build-release` job — moving it here means a release PR's *published* artifact builds **once** instead of twice. `docker.yml` now only builds on push-to-main (a config bump outside the release flow). The visual-regression legs are the one exception: they need `isCI`-frozen UI to match goldens, which the published (real-version) image can't provide, so the build job also builds a throwaway single-arch `version: CI` image and hands it to those legs the same way it hands off a fork build (`docker load` from an artifact) — see the E2E visual regression section below.
- **Fork PR** → fork tokens are read-only, so build single-arch, `docker save` to an artifact; downstream jobs `docker load` it (tagged `plenum-e2e`, the compose default).

The throwaway `ci-*` tags are pruned nightly by **`.github/workflows/ci-image-cleanup.yml`** (deletes `ci-*` versions older than `RETENTION_DAYS`; never `:latest` or semver tags). **`validate-release.yml`** also runs on `release/v*` PRs (one extra dry-run pass; click to start, since the bot PR won't auto-trigger it).

**Branch protection:** since the release build moved out of `docker.yml`, the required check for release PRs is now **Build (PR validation)** (container-ci), not the old **Build & Push release image**.

**Note:** All Python CI jobs install dependencies with `pip install ".[dev]"`, so
runtime deps (aiohttp, apscheduler, aiosqlite, apispec, swagger-ui-bundle, …) and test deps
(pytest, pytest-asyncio, pytest-cov, aioresponses, ruff, mypy) come from the
`[project]` and `[project.optional-dependencies] dev` tables in `pyproject.toml`.
Add any new test/runtime dependency there — the workflow picks it up automatically.
PyYAML is NOT a dependency; do not `import yaml` in code or tests.

---

## Key architectural facts

- **DB schema**: `system_settings` key-value table stores flags (`temperature_unit`, `unit_change_ack_required`, `unit_change_acked_unit`, `system_enabled`, `developer_mode`, `mcp_enabled`, `vacation_mode_enabled`, `vacation_mode_return_at`, log-retention keys, and more). No dedicated settings table — the full catalog with defaults and guards lives in the `plenum-config-and-flags` skill.
- **Engine**: `cycle_engine.py` calls `_set_thermostat_setpoint()` with pre-converted °F values. Never convert inside the engine — conversion is the API layer's job.
- **Scheduler**: Owns `_active_unit`. The engine and routes both read the unit through the scheduler, not directly from DB.
- **WebSocket**: `ws_handler.py` broadcasts zone status events. Temperatures in these events are raw °F — the frontend converts for display.
- **CSV export**: `/api/metrics/export.csv` uses `_from_f()` and labels headers with the active unit, e.g. `outside_temp_at_start (°C)`.
- **OpenAPI / Swagger** (`backend/api/openapi.py`, Issue #188): the spec is generated directly from `apispec` + `MarshmallowPlugin` over the marshmallow schemas; Swagger UI is served from the `swagger-ui-bundle` package (self-hosted, offline/ingress/CSP-safe). This replaced the abandoned `aiohttp-apispec`, which blocked marshmallow v4. The `@docs` / `@request_schema` / `@response_schema` decorators are **documentation-only** — no validation middleware is installed, so handlers still parse and validate `await request.json()` themselves. Every `/api/` route must carry `@docs` + a `@response_schema` (except `/api/backup` and `/api/metrics/export.csv`); `tests/test_api_spec_enforcement.py` fails the build otherwise.
- **Frontend form state**: Thermostat/Room/Schedule forms store values in **display units** (°C or °F as the user sees them). Initialize from API responses via `toDisplay` / `toDisplayDelta`; submit the raw value. See the "Frontend form contract" section above and `Thermostats.tsx` / `Rooms.tsx` / `Schedules.tsx` for the canonical implementation. **Never** call `toStorage` / `toStorageDelta` on outgoing payloads — that re-introduces the #231 double-conversion.

---

## Common pitfalls

1. **New temperature field in a write endpoint** — add `_to_f()` / `_delta_to_f()` call; add a `TestToF`-style unit test; add a Celsius-mode integration test that POSTs the field with a °C value and asserts the stored °F. Add the field to the matching frontend form and **send the raw display value** — do not call `toStorage` / `toStorageDelta` on the outgoing payload (see #231).
2. **New option in `config.yaml`** — also add `bashio::config` read and `export` in `run.sh` (enforced by `test_addon_config.py`).
3. **Displaying a temperature from entity state** — `EntityState.numeric` is always °F (backend normalises). Use `fmtTemp()`, not `${value}${unitLabel}`.
4. **Delta fields** — use `toDisplayDelta` (read) for delta fields like deadband / overshoot_delta / temp_offset. Using `toDisplay` (absolute) on a delta would subtract 32 °F and silently corrupt the displayed value.
5. **New pip dependency in a test** — add it to the `[project.optional-dependencies] dev` table in `pyproject.toml` (CI installs via `pip install ".[dev]"`); no workflow edit needed.
6. **ruff format** — run `ruff format backend/` before committing Python; the CI checks formatting separately from linting.
7. **Adding a frontend temperature write path** — register the new field in **both** manifests (`TEMPERATURE_FIELDS` in `routes.py` AND `temperature-fields.ts`), then extend `temperature-units.spec.ts` with a round-trip carrying a `// @covers: <field>` marker. The parity test (`test_temperature_field_parity.py`) fails CI if any of the three are out of sync — the matrix run under both °F and °C is the only end-to-end guard against the kind of double-conversion bug that escapes per-side unit tests.
8. **Changing any rendered UI** — the visual-regression suite (the `E2E visual regression (F)/(C)` jobs in `container-ci.yml`) will fail until goldens are regenerated. Regenerate **both** unit sets (`-Fahrenheit-` and `-Celsius-`); the matrix does this automatically on first-failure and commits them back, but review every changed PNG in the file diff like code. If your change adds **time-varying or engine-driven UI** (timers, feeds, wall-clock, live counts), wrap it in `<Frozen>` (`frontend/src/ci.tsx`) or goldens will never stabilise. Do not freeze static fixture values.
