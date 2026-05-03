# Plenum — Developer Notes for Claude

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
│   ├── pages/        # Dashboard, Rooms, Schedules, Thermostats, Settings,
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
**All temperatures are stored in °F** in SQLite and passed through the system as °F.
Conversion happens only at two boundaries:
- **API write boundary** (incoming): user input → °F for storage
- **Frontend display** (outgoing): °F from API → display unit for rendering

### Backend helpers (`backend/api/routes.py`)

```python
_to_f(value, unit)        # absolute temp, display unit → °F (2dp)
_delta_to_f(value, unit)  # delta (deadband, offset), display unit → °F (2dp)
_from_f(value, unit)      # °F → display unit (1dp), None → ""  [CSV export only]
```

**Every POST/PUT/PATCH endpoint** that accepts a temperature field must call one of these.
Fields that use `_to_f`: `target_temp`, `system_wide_temp`, `default_temp`, `min_setpoint`, `max_setpoint`
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
toStorage(displayValue)       // display unit → °F (2dp float)
toStorageDelta(displayDelta)  // display unit delta → °F (2dp float)
fmtTemp(fahrenheit)           // formatted string e.g. "21.1°C"
unitLabel                     // "°F" | "°C"
unit                          // "F" | "C"
isCelsius                     // boolean
```

**Absolute vs delta**: absolute uses `(f-32)*5/9`; delta uses only `f*5/9` (no offset).
This matters for deadband, overshoot_delta, temp_offset.

### SAFETY_FIELDS / FIELDS pattern (Thermostats.tsx, Settings.tsx)
Each field entry has a `kind: "absolute_temp" | "delta_temp" | "other"`.
The render loop uses `kind` to pick the right conversion function.
Labels get `(${unitLabel})` appended only for temp fields.

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
Coverage thresholds (in `vite.config.ts`): lines 79, functions 71, branches 77, statements 79.

**Test patterns:**
- Celsius mode: wrap with `<UnitContext.Provider value={buildUnitContext("C")}>`
- System context: wrap with `<SystemContext.Provider value={mockSystem}>`
- Mocks: `vi.mock("../api")` at module level, `vi.mocked(api.fn).mockResolvedValue(...)` per test

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
- **Frontend form state**: Thermostats/Settings forms store values internally as °F. Inputs display via `toDisplay`/`toDisplayDelta`, `onChange` converts back via `toStorage`/`toStorageDelta`. The form's `°F` state is sent to the backend as-is (no double conversion).

---

## Common pitfalls

1. **New temperature field in a write endpoint** — add `_to_f()` / `_delta_to_f()` call; add `TestToF`-style unit test; add Celsius-mode integration test.
2. **New option in `config.yaml`** — also add `bashio::config` read and `export` in `run.sh` (enforced by `test_addon_config.py`).
3. **Displaying a temperature from entity state** — `EntityState.numeric` is always °F (backend normalises). Use `fmtTemp()`, not `${value}${unitLabel}`.
4. **Delta fields** — use `toDisplayDelta`/`toStorageDelta`, not `toDisplay`/`toStorage`. Using the wrong one adds/subtracts 32.
5. **New pip dependency in a test** — also add it to the `pip install` line in `.github/workflows/lint.yml`.
6. **ruff format** — run `ruff format backend/` before committing Python; the CI checks formatting separately from linting.
