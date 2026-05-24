# Plenum — Developer Notes for Claude

## Workflow conventions

- **After every push to a PR**: always update the PR body with a description of what changed, why, and a test plan. Do this every time without being asked.
- **When reviewing a PR and pushing fixes**: push directly to the PR's own branch (not to a separate review branch), so the fixes appear in the PR diff.

## What this repo is

A Home Assistant add-on that replaces the Flair cloud app. It controls HA `cover` entities (vents) while reading temperature from HA `sensor` entities and commanding HA `climate` entities (thermostats). The UI is a React SPA served via HA ingress.

```
smart_vent/
├── backend/          # Python 3.12, aiohttp, asyncio
│   ├── api/routes.py # All REST endpoints (~1500 lines)
│   ├── scheduler.py  # Orchestrator: unit detection, DB init, APScheduler jobs
│   ├── engine/       # cycle_engine.py, room_manager.py, vent_controller.py
│   ├── ha_client.py  # Raw HA WebSocket API client
│   ├── db.py         # aiosqlite helpers (~2000 lines)
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

### Backend helpers (`backend/api/routes.py`)

```python
_to_f(value, unit)        # absolute temp, display unit → °F (2dp)
_delta_to_f(value, unit)  # delta (deadband, offset), display unit → °F (2dp)
_from_f(value, unit)      # °F → display unit (1dp), None → ""  [CSV export only]
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
Coverage threshold: **90%** (`pyproject.toml` `fail_under = 90`).

**Test patterns:**
- Unit tests for pure helpers: `tests/test_routes_helpers.py` (TestToF, TestDeltaToF, TestFromF)
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
Coverage thresholds (in `vite.config.ts`): lines 80.9, functions 71.1, branches 77.1, statements 80.9.

**Test patterns:**
- Celsius mode: wrap with `<UnitContext.Provider value={buildUnitContext("C")}>`
- System context: wrap with `<SystemContext.Provider value={mockSystem}>`
- Mocks: `vi.mock("../api")` at module level, `vi.mocked(api.fn).mockResolvedValue(...)` per test
- **Temp-write assertion (#231)**: in Celsius mode, assert the POST/PUT body contains the user's **raw display value** (e.g. `min_setpoint: 16`), NOT the pre-converted °F. The backend converts; the frontend must not.

### E2E round-trip (`e2e/tests/temperature-units.spec.ts`)
Matrix-runs against both °F and °C stacks via `.github/workflows/e2e-conversion.yml`. Each test edits a temperature field through the UI, reloads, and asserts the field shows exactly the value the user typed. This is the only place the full conversion contract is exercised end-to-end, and would have caught the #231 double-conversion bug — neither side's unit tests did.
- °C run uses `docker-compose.test.celsius.yml` as an override on top of `docker-compose.test.yml` to set `TEMPERATURE_UNIT=C`.
- `PLENUM_TEMP_UNIT` env var tells the spec which unit the stack is in so the assertion values are scaled accordingly.

### Temperature field registry (parity-enforced)
Every temperature field on a write boundary is registered in two manifests kept in lockstep:
- **Python**: `TEMPERATURE_FIELDS` dict in `smart_vent/backend/api/routes.py` (`field` → `kind`).
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
| Python (ruff) | `ruff check backend/` + `ruff format --check backend/` |
| Python (pytest) | `pip install pytest pytest-asyncio pytest-cov aiosqlite aiohttp apscheduler python-dotenv` then pytest |
| Frontend (ESLint + Prettier) | `npm run lint` + `npm run format:check` + `npm run test:coverage` |
| Build (PR validation) | Docker build check |

**Note:** The CI pytest install is a manual `pip install` list, **not** `pip install -e .[dev]`.
Do not add test dependencies that require extra packages not in that list — or update the workflow too.
PyYAML is NOT installed in CI.

---

## Key architectural facts

- **DB schema**: `system_settings` key-value table stores flags (`temperature_unit`, `unit_change_ack_required`, `system_enabled`, `developer_mode`). No dedicated settings table.
- **Engine**: `cycle_engine.py` calls `_set_thermostat_setpoint()` with pre-converted °F values. Never convert inside the engine — conversion is the API layer's job.
- **Scheduler**: Owns `_active_unit`. The engine and routes both read the unit through the scheduler, not directly from DB.
- **WebSocket**: `ws_handler.py` broadcasts zone status events. Temperatures in these events are raw °F — the frontend converts for display.
- **CSV export**: `/api/metrics/export.csv` uses `_from_f()` and labels headers with the active unit, e.g. `outside_temp_at_start (°C)`.
- **Frontend form state**: Thermostat/Room/Schedule forms store values in **display units** (°C or °F as the user sees them). Initialize from API responses via `toDisplay` / `toDisplayDelta`; submit the raw value. See the "Frontend form contract" section above and `Thermostats.tsx` / `Rooms.tsx` / `Schedules.tsx` for the canonical implementation. **Never** call `toStorage` / `toStorageDelta` on outgoing payloads — that re-introduces the #231 double-conversion.

---

## Common pitfalls

1. **New temperature field in a write endpoint** — add `_to_f()` / `_delta_to_f()` call; add a `TestToF`-style unit test; add a Celsius-mode integration test that POSTs the field with a °C value and asserts the stored °F. Add the field to the matching frontend form and **send the raw display value** — do not call `toStorage` / `toStorageDelta` on the outgoing payload (see #231).
2. **New option in `config.yaml`** — also add `bashio::config` read and `export` in `run.sh` (enforced by `test_addon_config.py`).
3. **Displaying a temperature from entity state** — `EntityState.numeric` is always °F (backend normalises). Use `fmtTemp()`, not `${value}${unitLabel}`.
4. **Delta fields** — use `toDisplayDelta` (read) for delta fields like deadband / overshoot_delta / temp_offset. Using `toDisplay` (absolute) on a delta would subtract 32 °F and silently corrupt the displayed value.
5. **New pip dependency in a test** — also add it to the `pip install` line in `.github/workflows/lint.yml`.
6. **ruff format** — run `ruff format backend/` before committing Python; the CI checks formatting separately from linting.
7. **Adding a frontend temperature write path** — register the new field in **both** manifests (`TEMPERATURE_FIELDS` in `routes.py` AND `temperature-fields.ts`), then extend `temperature-units.spec.ts` with a round-trip carrying a `// @covers: <field>` marker. The parity test (`test_temperature_field_parity.py`) fails CI if any of the three are out of sync — the matrix run under both °F and °C is the only end-to-end guard against the kind of double-conversion bug that escapes per-side unit tests.
