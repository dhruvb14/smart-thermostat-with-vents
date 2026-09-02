---
name: plenum-validation-and-qa
description: How to run and add tests in Plenum (pytest/vitest/Playwright run commands, the --no-cov trap), what counts as evidence in the repo (smart-thermostat-with-vents), and how to add tests CI will accept. Load when running, writing, or debugging tests, when a coverage gate or parity/enforcement test fails (test_temperature_field_parity, test_addon_config, test_api_spec_enforcement), when you need the Celsius-mode test patterns, when adding an E2E round-trip with @covers tags, or when regenerating golden screenshots.
---

# Plenum Validation and QA

Evidence standards for this repo: the four test layers, the enforcement tests
that police cross-file contracts, the coverage/golden acceptance thresholds,
and step-by-step checklists for adding each kind of test.

**When NOT to use this skill:**
- Which gates a *class of change* must clear and what a reviewer expects → `plenum-change-control`.
- CI workflow internals, container-ci build modes, the golden auto-commit bot → `plenum-ci-and-release`.
- Why the °F-storage / write-boundary contract exists (#231 story) → `plenum-architecture-contract` (invariant) and `plenum-failure-archaeology` (incident).
- Installing the toolchain from scratch → `plenum-build-and-env`.
- Running the app to reproduce a runtime bug → `plenum-debugging-playbook`.

**Jargon (defined once):**
- *Write boundary* — the POST/PUT/PATCH handlers in `smart_vent/backend/api/routes.py`, the only place display-unit temperatures become °F.
- *Delta field* — a temperature difference (deadband, temp_offset, overshoot_delta); °C→°F is `×9/5` with **no** +32 offset.
- *Golden* — a committed reference PNG in `e2e/screenshots/` that the visual suite pixel-diffs against.
- *Round-trip test* — type a value in the UI, save, reload, assert the field shows exactly what was typed. The only test shape that catches double-conversion (#231).
- *Parity test* — a pytest that reads *other files* (TS manifests, YAML, shell) and fails when two declarations of the same fact drift apart.

---

## 1. Test inventory — four layers

| Layer | Where | Runner | What it can catch | What it cannot |
|---|---|---|---|---|
| Backend unit | `smart_vent/backend/tests/test_*.py` | pytest | pure-helper math, engine logic, parity/enforcement rules | anything crossing the HTTP or UI boundary |
| Backend integration | `smart_vent/backend/tests/integration/` | pytest + aiohttp `TestClient` + `FakeHomeAssistant` | full request→DB→engine flows, Celsius write-boundary conversion | frontend behavior |
| Frontend unit | `smart_vent/frontend/src/**/*.test.ts(x)` | vitest + React Testing Library | rendering, form state, *what the UI puts on the wire* | whether the backend agrees with that wire format |
| E2E | `e2e/tests/*.spec.ts` | Playwright vs docker-compose stack | the cross-boundary contract itself: round-trip (conversion) + visual goldens (rendering) | fast feedback — minutes, not seconds |

The #231 lesson (see `plenum-failure-archaeology`): each side's unit tests
passed while the system double-converted, because each side asserted a
different contract. Per-side tests are necessary; only the E2E round-trip is
sufficient for conversion correctness.

### Run commands (re-verified 2026-09-01, v0.35.0; superseded 2026-07-04/v0.22.1 figures below are historical)

Backend — needs Python **3.12+** (`requires-python = ">=3.12"` in `smart_vent/pyproject.toml`; a 3.11 venv fails at pip-install time):

```bash
# venv setup (once) → plenum-build-and-env; then, with the venv active:
cd smart_vent
python -m pytest backend/tests/ -v            # full suite (what lint.yml runs)
# Executed 2026-07-04 (v0.22.1): 918 passed in ~83 s, coverage 93.64% (gate 92.5%)
# — gate has since ratcheted to 96.7%, see §6; re-verify the pass count/timing
# before citing them as current.
```

**TRAP — partial runs need `--no-cov`.** `pyproject.toml` sets
`addopts = "--cov=backend --cov-report=term-missing"` and
`fail_under = 96.7`, so a file-scoped run reports ~0–1% coverage and exits
nonzero *even when every test passes* ("FAIL Required test coverage of 96.7%
not reached" — reproduced 2026-09-01: `test_units.py` alone gives "24 passed"
but "Total coverage: 0.23%"). Always:

```bash
python -m pytest backend/tests/test_units.py --no-cov -q
python -m pytest backend/tests/integration/test_rooms_api.py --no-cov -q
```

Frontend:

```bash
cd smart_vent/frontend
npx vitest run                          # full suite: 33 files / 569 tests, ~21 s (as of 2026-09)
npx vitest run src/contexts.test.ts     # single file
npm run test:coverage                   # what CI (lint.yml) runs; enforces thresholds
```

E2E (Playwright, needs the docker-compose stack — see `e2e/README.md` for the
full bring-up: `docker compose -f docker-compose.test.yml up --wait homeassistant`,
`python3 e2e/scripts/setup-ha.py`, start `plenum` with `HA_TOKEN`, then
`cd e2e && npm ci && npx playwright install chromium`):

```bash
cd e2e
PLENUM_TEMP_UNIT=F npx playwright test temperature-units.spec.ts --project=chromium  # round-trip (what container-ci's conversion job runs)
npx playwright test --grep-invert "Temperature round-trip"                            # visual suite (what the e2e job runs)
npm run test:update                                                                   # regenerate goldens (--update-snapshots)
```

The round-trip spec is excluded from the visual suite because it mutates
shared backend state (creates schedules) and would not survive the visual
job's two projects + update→verify double pass.

---

## 2. Backend test patterns

### Unit tests: `test_units.py` (NOT `test_routes_helpers.py` — renamed)

The conversion helpers live in **`smart_vent/backend/units.py`**
(pre-2026-07-05 CLAUDE.md copies said routes.py; corrected in PR #388 — if
docs and repo disagree again, the repo wins). Four functions: `to_f`, `delta_to_f`
(display→°F, 2dp), `from_f`, `from_f_delta` (°F→display, 1dp).
`routes.py` imports them as `_to_f` / `_delta_to_f` / `_from_f` /
`_from_f_delta` (lines 35–38). `backend/tests/test_units.py` holds 24 tests
in `TestToF` / `TestDeltaToF` / `TestFromF` / `TestFromFDelta` style classes —
copy its shape for new conversion-adjacent helpers, e.g.:

```python
def test_celsius_delta_does_not_add_offset(self):
    # +32 must NOT be applied to deltas — 1°C delta = 1.8°F, not 33.8°F
    assert delta_to_f(1.0, "C") == 1.8
```

### Integration fixtures (`backend/tests/integration/conftest.py`)

| Fixture | Gives you |
|---|---|
| `fake_ha` | `FakeHomeAssistant` (`integration/fake_ha.py`) — in-memory HAClient mirror; records every service call in `.calls` (list of `ServiceCall(domain, service, data)`), dispatches state-change subscriptions, has `ha_temp_unit` ("F" default; set "C" + seed °C climate states for HA-side Celsius tests) |
| `db_path` | temp SQLite file, wal/shm cleaned up after |
| `app` | full aiohttp app via `build_app(fake_ha, db_path, frontend_dist=None, start_ha=False)` |
| `client` | started `aiohttp.test_utils.TestClient` against that app |
| `tick` | awaitable that runs one full scheduler tick (`scheduler._tick_all()`) — APScheduler's 60 s job never fires inside a short test, so **you** advance the engine |

The top-level `backend/tests/conftest.py` only pins `TZ=UTC` (timezone-drift
tests depend on it).

### The Celsius `_active_unit` pattern (write-boundary conversion evidence)

The active display unit is held on the scheduler; flip it, POST a °C value,
assert the **stored °F**, restore in `finally`. Canonical example —
`integration/test_rooms_api.py::test_room_ambient_suppression_celsius_delta_conversion`:

```python
client.app["scheduler"]._active_unit = "C"
try:
    room_id = await _create_room(client,
        ambient_suppression_min_differential=2,   # 2°C delta
        ambient_suppression_deadband=2)
finally:
    client.app["scheduler"]._active_unit = "F"    # ALWAYS restore
conn = client.app["scheduler"]._db_conn
room = await _db.get_room(conn, room_id)
assert room.ambient_suppression_min_differential == 3.6   # 2°C×9/5, no +32
```

For an absolute field the expected value includes the +32 (16°C → 60.8°F).
Forgetting the `finally` restore poisons every later test in the module.

---

## 3. Frontend test patterns (vitest + RTL)

- Mock the API module at file top: `vi.mock("../api")`, then per-test
  `vi.mocked(api.getThermostats).mockResolvedValue(mockThermostats)`;
  `vi.clearAllMocks()` in `beforeEach`.
- Celsius mode: wrap in the unit context with the **pure factory** (no app
  bootstrapping needed): `<UnitContext.Provider value={buildUnitContext("C")}>…</UnitContext.Provider>`
  (`buildUnitContext` from `src/contexts.ts`).
- Pages needing system state: `<SystemContext.Provider value={mockSystem}>` (see `src/pages/Dashboard.test.tsx`).

### The #231 raw-display-value write assertion (mandatory for any temp form)

In Celsius mode, assert the outgoing payload carries the user's **raw °C
number**, and explicitly assert it is NOT the pre-converted °F. Canonical:
`src/pages/Thermostats.test.tsx`, "never POSTs pre-converted °F when in
Celsius mode (#231)":

```tsx
fireEvent.change(screen.getByLabelText(/Min setpoint \(°C\)/i), { target: { value: "16" } });
fireEvent.click(within(card).getByText("Save changes"));
await waitFor(() => expect(api.updateThermostat).toHaveBeenCalled());
const [, payload] = vi.mocked(api.updateThermostat).mock.calls[0];
expect(payload.min_setpoint).toBe(16);        // raw display value
expect(payload.min_setpoint).not.toBe(60.8);  // guard the buggy pre-converted °F
```

Sibling assertions: delta fields send the raw °C delta (`deadband: 1`, not
`1.8`); labels get `(°C)` appended only on temp fields; init values are
`toDisplay`-converted (60°F renders as 15.6).

---

## 4. E2E round-trip (`e2e/tests/temperature-units.spec.ts`)

- Parameterised on `PLENUM_TEMP_UNIT` (F default): under °F the round-trip is
  trivial; under °C it is *the* #231 regression test. Values are chosen to
  round-trip cleanly through 2dp-°F storage (e.g. deadband 0.5°C → 0.9°F).
- Every test carries a `// @covers: field[, field…]` line as the **first
  non-whitespace on its own line** (the parity test's regex requires that);
  6 marker lines cover all 19 fields as of 2026-09.
- Shape: fill inputs by id (`[id="thermo-<entity_id>-<suffix>"]` — attribute
  selector, because entity ids contain dots), capture the PUT response and
  surface its body in the assertion message, save, `page.reload()`, read back
  with `toBeCloseTo(typedValue, 1)`.
- Runs in container-ci's `Round-trip (F)/(C)` matrix legs
  (`npx playwright test temperature-units.spec.ts --project=chromium`); the °C
  leg layers `docker-compose.test.celsius.yml` (sets `TEMPERATURE_UNIT: "C"`
  on the plenum service only — env var outranks HA's unit in the detection
  chain) and the job asserts `GET /api/settings` reports the matrix unit
  before testing.

---

## 5. The enforcement/parity tests (run them; know their failure text)

All three live in `smart_vent/backend/tests/` and pass in 0.5 s
(`--no-cov -q`, executed 2026-09-01: 33 passed together with test_units.py).

### `test_temperature_field_parity.py` — five rules, three files in lockstep

The three declarations of "which keys are temperatures":
1. `TEMPERATURE_FIELDS` **dict** in `smart_vent/backend/api/routes.py` (~line 425), `field → kind`.
2. `TEMPERATURE_FIELDS` **array** in `e2e/tests/temperature-fields.ts` (`field`, `kind`, `ui`, `endpoints`).
3. `// @covers:` markers in `e2e/tests/temperature-units.spec.ts`.

Kinds (as of 2026-09; don't confuse them with the frontend SAFETY_FIELDS kinds
`absolute_temp/delta_temp/other`, which are a separate, still-correct
vocabulary in `Thermostats.tsx`):
`absolute`, `absolute_nullable` (null clears), `delta`, `delta_nullable`.
19 registered fields (grew from 12 as of 2026-07 with the Eco Mode #404
thresholds/drift/hysteresis fields): default_temp, min_setpoint, max_setpoint,
deadband, overshoot_delta, cooling_lockout_below_f, system_wide_temp,
temp_offset, deadband_override, ambient_suppression_min_differential,
ambient_suppression_deadband, eco_cooling_outdoor_threshold,
eco_cooling_full_drift_temp, eco_cooling_max_drift,
eco_heating_outdoor_threshold, eco_heating_full_drift_temp,
eco_heating_max_drift, eco_hysteresis_band, target_temp.

| Test | Fails when | Failure message starts with |
|---|---|---|
| `test_python_and_ts_manifests_have_identical_field_sets` | field in one manifest only | "Temperature field registry drift:" (lists `in routes.py only` / `in temperature-fields.ts only`) |
| `test_python_and_ts_kinds_agree` | same field, different kind (silent data corruption: `_to_f` applies +32, `_delta_to_f` doesn't) | "Temperature field kind mismatch between Python and TS manifests:" |
| `test_every_ui_field_has_an_e2e_covers_marker` | `ui: true` entry with no `@covers:` mention | "UI-writable fields with no e2e @covers marker:" |
| `test_no_orphan_covers_markers` | `@covers:` names an unknown field | "@covers markers referencing unknown fields:" |
| `test_every_registered_field_appears_in_routes_source` | dead registry entry never referenced in routes.py | "TEMPERATURE_FIELDS keys missing from routes.py source:" |

Also: the TS manifest is parsed by **regex** (`_ENTRY_RE`). Adding a new
property to `TempField` or reordering properties breaks parsing and trips the
guard assert "No entries parsed from … the regex in this test is likely out
of sync". Keep entry property order `field, kind, ui, endpoints`.

### `test_addon_config.py` — config.yaml ↔ run.sh parity

Every key under `options:` in `smart_vent/config.yaml` must have either
`bashio::config '<key>'` **or** `get_config '<key>'` in `smart_vent/run.sh`.
Failure: "Option(s) defined in config.yaml but never read in run.sh via
bashio::config or get_config: [...]". Parses YAML by regex (PyYAML is
deliberately not a dependency — do not `import yaml`).

### `test_api_spec_enforcement.py` — OpenAPI completeness

Every route whose path starts `/api/` must (a) have `@docs` metadata
(`handler.__apispec__`) and (b) document a 200/201 response via
`@response_schema` — exceptions: `/api/backup`, `/api/metrics/export.csv`.
Failures: "The following endpoints are missing @docs decorators: […]" /
"…missing @response_schema decorators: […]". The decorators are
documentation-only; handlers still validate `await request.json()` themselves.

---

## 6. Acceptance thresholds — ratchet-only discipline

Verified in-repo 2026-09-01 (v0.35.0; CLAUDE.md matches):

| Gate | Value | Where |
|---|---|---|
| Backend coverage | `fail_under = 96.7` | `smart_vent/pyproject.toml` `[tool.coverage.report]` |
| Frontend coverage | lines 94.2, functions 91.3, branches 79.9, statements 92.0 | `smart_vent/frontend/vite.config.ts` `test.coverage.thresholds` (comment notes they're calibrated for Vitest 4's v8 AST-aware remapping) |
| Golden pixel diff | `maxDiffPixels: 100` global | `e2e/playwright.config.ts` `expect.toHaveScreenshot` |
| Golden pixel diff, high-DPI/native-widget specs | `maxDiffPixels: 800` | Per-spec override — `e2e/tests/metrics.spec.ts` (high-DPI mobile `deviceScaleFactor: 3` amplifies native `<input type="date">` jitter ~9×) is the original case; the same override is now also used for several delete/revoke confirmation-modal screenshots (`thermostats.spec.ts`, `schedules.spec.ts`, `rooms.spec.ts`, `logs.spec.ts`, `auth.spec.ts`) |

Rules of the house:
- **Coverage thresholds only go up.** Never lower a gate to make a PR pass;
  add tests. If a legitimate architectural change strands coverage, raising it
  back is part of the same PR's evidence.
- **Goldens are reviewed like code.** Every changed PNG in a PR diff gets
  eyeballed — the auto-commit bot regenerates them, it does not approve them
  (bot mechanics → `plenum-ci-and-release`).
- **Prefer a per-spec `maxDiffPixels` bump over masking** selectors, so the
  rest of the page stays pixel-checked (per-selector masks were tried and
  rejected — see `plenum-failure-archaeology`).
- New volatile UI (clocks, countdowns, live feeds) must go through
  `<Frozen>` in `frontend/src/ci.tsx` (single `isCI` branch =
  `import.meta.env.VITE_APP_VERSION === "CI"`, tested both ways in
  `ci.test.tsx`); do NOT freeze static fixture values, and do NOT branch on
  `isCI` inline in pages (tanks branch coverage).

---

## 7. Golden and fixture inventory

- **Goldens**: 92 PNGs in `e2e/screenshots/` (as of 2026-07). Flat naming via
  `snapshotPathTemplate` = `{arg}-${UNIT_LABEL}-{projectName}.png`, e.g.
  `dashboard-Fahrenheit-chromium.png`, `metrics-Celsius-mobile.png`.
  Two units (Fahrenheit/Celsius from `PLENUM_TEMP_UNIT`) × two projects
  (`chromium` desktop 1280×900, `mobile` = iPhone 14 viewport forced to
  chromium browser).
- **HA fixture**: `e2e/fixtures/ha-config/configuration.yaml` — template
  sensors (fixed °F values 68.5–80.0), two `generic_thermostat`s over dummy
  `input_boolean` switches, four template covers. **`unit_system:
  us_customary` is pinned** — without it HA YAML-config defaults to metric and
  reads `target_temp: 70` as 70 °C, which Plenum's correct normalisation
  surfaces as a nonsensical 158 °F (that was a real bug; the pin comment tells
  the story). HA itself is always °F; the matrix varies only Plenum's display unit.
- **Compose stacks** (repo root): `docker-compose.test.yml` (ha-init copies
  the fixture config → homeassistant 2025.5.3 → plenum, image
  `${PLENUM_IMAGE:-plenum-e2e}`, `TEMPERATURE_UNIT: "F"`), layered with
  `docker-compose.test.celsius.yml` for the °C legs.
- **`e2e/tests/fixtures.ts`**: auto-fixture that hides `.nav-version` so
  version bumps don't invalidate every golden — import `test`/`expect` from it,
  not from `@playwright/test`, in every spec.
- **`e2e/global-setup.ts`**: seeds the addon by clicking through the real UI
  (EntityPicker needs a live HA behind `/api/ha/entities`); schedules are
  seeded via REST.

---

## 8. HOW-TO checklists

### A. Add a backend unit test
1. Put it in `smart_vent/backend/tests/test_<topic>.py`; class-per-helper
   (`TestToF` style) for pure functions.
2. Run scoped: `cd smart_vent && python -m pytest backend/tests/test_<topic>.py --no-cov -q` (venv active; setup → `plenum-build-and-env`).
3. Before pushing, run the full suite *with* coverage (drop `--no-cov`) —
   the 96.7% gate is on the whole run.
4. New test-only dependency? Add to `[project.optional-dependencies] dev` in
   `smart_vent/pyproject.toml` (CI installs `pip install ".[dev]"`). Never PyYAML.

### B. Add a Celsius integration test for a new temperature field
1. Register the field first: `TEMPERATURE_FIELDS` dict in `routes.py` AND
   array entry in `e2e/tests/temperature-fields.ts` (same `kind`), plus the
   `_to_f`/`_delta_to_f` call in the handler (checklist owned by
   `plenum-change-control`; validation bounds go AFTER normalization, on the
   °F value — `.jules/sentinel.md`, enforced by `integration/test_sentinel_validation.py`).
2. In `backend/tests/integration/`, use the `client` fixture; flip
   `client.app["scheduler"]._active_unit = "C"` in try/`finally` restore "F".
3. POST/PUT a °C value; read the stored row via
   `client.app["scheduler"]._db_conn` + the `db.py` getter; assert the exact
   °F: absolute `v*9/5+32` (2dp), delta `v*9/5` (2dp).
4. Run: `pytest backend/tests/integration/test_<file>.py --no-cov -q`, then
   `pytest backend/tests/test_temperature_field_parity.py --no-cov -q` — it
   will fail until step 1 and the `@covers` tag (checklist D) are complete.

### C. Add a frontend form test
1. Colocate as `src/pages/<Page>.test.tsx`. `vi.mock("../api")` at top;
   mock every fetch the page fires on mount (a missed one = hung `findBy*`).
2. Fahrenheit block + a separate `describe("… — Celsius mode")` wrapping
   render in `buildUnitContext("C")`.
3. Include the three #231 assertions: init shows `toDisplay`-converted value;
   payload carries the raw display number; payload `.not.toBe(<°F value>)`.
4. `cd smart_vent/frontend && npx vitest run src/pages/<Page>.test.tsx`, then
   `npm run test:coverage` before pushing (thresholds are on the full run).

### D. Add an E2E round-trip with @covers
1. In `temperature-units.spec.ts`: add a value constant pair
   (`isCelsius ? "<°C>" : "<°F>"`) that round-trips cleanly through 2dp-°F
   storage; add/extend a test that fills → saves (assert PUT status with body
   text in the message) → reloads → `toBeCloseTo` reads back.
2. First line inside the test body: `// @covers: <field>` (line-initial —
   the parity regex ignores indented-in-prose mentions inside strings but
   requires the marker to start its own line).
3. Set `ui: true` on the field's `temperature-fields.ts` entry.
4. Verify cheaply without Docker: run the parity test (rule 3 and 4 police
   your marker). Full verification: the compose stack + the two
   `PLENUM_TEMP_UNIT` runs from section 1.

### E. Regenerate goldens
1. Bring up the Docker stack (goldens generated without real HA show "—"
   everywhere and will fail CI — `e2e/README.md` warning).
2. `cd e2e && npm run test:update` under `PLENUM_TEMP_UNIT=F`, then repeat
   with the celsius compose overlay and `PLENUM_TEMP_UNIT=C` — **both** unit
   sets, all four name variants per page.
3. Review every changed PNG like code; commit.
4. Or let CI do it: push, the container-ci `e2e` legs upload `goldens-F`/`goldens-C`
   artifacts and the `commit-goldens` fan-in job pushes one combined commit
   back to your branch (details → `plenum-ci-and-release`; pre-2026-07-05
   CLAUDE.md copies described the older `max-parallel: 1` / same-job-verify
   design — corrected in PR #388).
5. If a golden won't stabilise between the update and verify passes, you have
   un-frozen volatile UI — wrap it in `<Frozen>` (section 6).

---

## 9. Evidence expectations for engine/safety PRs

Full gating matrix → `plenum-change-control`. The QA-side summary:

- Every protective behavior (short-cycle protection, cooling lockout,
  staleness guard, setpoint backstop, comfort envelope) has a named
  integration test under `backend/tests/integration/test_safety_*.py`,
  `test_short_cycle_protection.py`, `test_outdoor_cooling_lockout.py`,
  `test_sensor_staleness.py`, `test_setpoint_bounds.py` etc. Touching a guard
  means touching (or consciously preserving) its test — a guard PR without a
  failing-before/passing-after test is not evidence, it's a claim.
- Engine changes are exercised by *manually ticked* integration tests
  (`tick` fixture) that assert on `fake_ha.calls` — the recorded
  `ServiceCall` list is the ground truth for "what would HA have been told".
- Characterization first: for behavior you're about to change, follow
  `integration/test_trigger_change_characterization.py`'s pattern — pin
  current behavior in a test, then change it deliberately.
- Anything that alters rendered UI additionally owes regenerated dual-unit
  goldens (checklist E) reviewed in the diff.

---

## Provenance and maintenance

§6 (coverage/golden thresholds), the run commands, the parity/enforcement
test names, and the Celsius-mode snippets were re-verified by reading repo
files and **executing** commands on 2026-09-01 (v0.35.0, Python 3.12 venv,
vitest 4.1.10, pytest 9.1.1). Facts elsewhere in this skill not called out
below are carried over from the 2026-07-04 (v0.22.1) pass and have not been
individually re-checked since.

- Executed 2026-09-01: the three enforcement tests + `test_units.py`
  (`--no-cov -q` → 33 passed, 0.49 s); the `--no-cov` trap reproduced
  (`test_units.py` alone: 24 passed but exit-fail "Required test coverage of
  96.7% not reached. Total coverage: 0.23%"); read (not re-executed) the
  Celsius integration test `test_room_ambient_suppression_celsius_delta_conversion`
  and the frontend `Thermostats.test.tsx` #231 write-assertion test — both
  still match the patterns in §2/§3 verbatim; `npx vitest run
  src/contexts.test.ts` (17 passed) and the full frontend suite (33 files /
  569 tests, ~21 s) — `npm run test:coverage` also completed clean, coverage
  92.34%/91.39%/81.29%/94.49% (statements/functions/branches/lines) against
  the §6 gates (94.2/91.3/79.9/92.0 — measured comfortably above, consistent
  with the "ratcheted just below measured" comment in `vite.config.ts`).
- NOT re-run 2026-09-01: the full backend pytest suite with coverage (long-
  running; the 2026-07-04 figures below are stale and shown for trend only —
  re-run `python -m pytest backend/tests/` before citing a current pass
  count/coverage %). Playwright/docker E2E commands are transcribed from
  `e2e/README.md` and `container-ci.yml` (the `conversion` job "Round-trip
  (${{ matrix.unit }})" at line ~500, its run step at line ~625), not run (no
  Docker here). `npm run test:coverage`'s CI invocation is `lint.yml` line
  ~156 (was cited as line 88; corrected 2026-09-01).
  Historical (2026-07-04, v0.22.1): full backend pytest run 918 passed,
  ~83 s, coverage 93.64% against the then-gate of 92.5%; frontend
  `npm run test:coverage`'s v8 temp-file collection crashed in that sandbox
  (ENOENT `coverage/.tmp/coverage-0.json`) — an environment quirk, not a repo
  bug.
- CLAUDE.md drift history: pre-2026-07-05 CLAUDE.md copies said the helpers
  lived in routes.py (`test_routes_helpers.py`), described a standalone
  `e2e.yml` with `max-parallel: 1`, and quoted the old coverage numbers —
  all corrected in PR #388. Current repo truth: helpers in
  `backend/units.py` (`test_units.py`); parity kinds
  `absolute|absolute_nullable|delta|delta_nullable`; visual suite in
  `container-ci.yml` (`e2e` + `commit-goldens` jobs, plus a newer `e2e-auth`
  job not yet described elsewhere in this skill); coverage gates per §6.
  If docs and repo disagree again, the repo wins.

Re-verify volatile facts:

```bash
# Coverage gates
grep -n fail_under smart_vent/pyproject.toml
grep -n -A6 "thresholds" smart_vent/frontend/vite.config.ts
# Field registry size / kinds
grep -n -A20 "^TEMPERATURE_FIELDS" smart_vent/backend/api/routes.py
# @covers markers vs fields
grep -n "@covers" e2e/tests/temperature-units.spec.ts
# Golden count and tolerances
ls e2e/screenshots | wc -l
grep -rn maxDiffPixels e2e/playwright.config.ts e2e/tests/
# Enforcement tests still green (from smart_vent/)
python -m pytest backend/tests/test_temperature_field_parity.py \
  backend/tests/test_addon_config.py backend/tests/test_api_spec_enforcement.py --no-cov -q
# Full-suite counts + coverage
python -m pytest backend/tests/ -q | tail -3
cd frontend && npm run test:coverage
```
