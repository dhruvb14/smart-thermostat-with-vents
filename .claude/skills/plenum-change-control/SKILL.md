---
name: plenum-change-control
description: Load this BEFORE making any change to the Plenum repo (smart-thermostat-with-vents) — adding a temperature field, config option, endpoint, dependency, UI change, or engine/safety-guard edit — or before opening/reviewing/merging a PR or cutting a release. Explains how changes are classified, which CI gates each class must clear, what evidence a reviewer expects, the non-negotiable rules with the historical incident behind each, and the release/branch-protection flow.
---

# Plenum Change Control

How changes are classified, gated, and reviewed in this repo. Every rule here
has a rationale and, where one exists, the historical incident that created it.
Nothing in this skill contradicts `CLAUDE.md` (the project rulebook); where the
repo's actual files have drifted ahead of `CLAUDE.md`'s prose, the drift is
flagged explicitly — **the repo files win**.

**When NOT to use this skill:**
- Diagnosing a bug or symptom → load `plenum-debugging-playbook`.
- The story of a past incident in depth → `plenum-failure-archaeology`.
- Why an invariant exists architecturally (e.g. the full °F storage contract) → `plenum-architecture-contract`.
- Mechanics of workflows/build modes/golden auto-commit → `plenum-ci-and-release`.
- How to write the tests a gate demands (Celsius-mode patterns, coverage) → `plenum-validation-and-qa`.
- Adding a config knob end-to-end → `plenum-config-and-flags` (this skill only tells you which gates it must clear).

**Jargon used below** (defined once):
- *Write boundary* — the backend POST/PUT/PATCH handlers in
  `smart_vent/backend/api/routes.py`; the ONLY place display-unit temperatures
  are converted to °F before storage.
- *Delta field* — a temperature difference (deadband, temp_offset,
  overshoot_delta): °C→°F conversion is `×9/5` with NO `+32` offset.
- *Deadband* — the tolerance band around a target temperature within which the
  engine takes no action.
- *Golden* — a committed reference screenshot PNG in `e2e/screenshots/`; the
  visual-regression suite fails on any pixel deviation from it.
- *Parity test* — a test that fails CI when two files that must stay in
  lockstep drift apart.
- *Short-cycling* — starting/stopping HVAC equipment within minutes; a primary
  compressor failure mode.
- *Release PR* — the bot-opened PR from a `release/vX.Y.Z` branch created by
  `release-pr.yml` when a semver tag is pushed.

---

## 1. The non-negotiables (rule → rationale → incident)

| # | Rule (from CLAUDE.md unless noted) | Rationale | Incident behind it |
|---|---|---|---|
| 1 | **Never leak exception detail into API responses.** In route-handler `except` blocks: `log.exception("…context…")` then `return error("generic message", status=5xx)`. Never embed `{exc}` / `str(exc)` in the body. | Raw exception strings disclose internals (paths, SQL, tokens) — CWE-209 information disclosure. | **Security alert #4** (GitHub code-scanning alert; not an issue — detail lives in CLAUDE.md). `routes.py` has 5 `log.exception` call sites following the pattern (as of 2026-07, v0.22.1). |
| 2 | **The frontend NEVER converts temperatures on outgoing payloads** (no `toStorage`/`toStorageDelta` on POST/PUT bodies). Conversion happens exactly once, at the backend write boundary via `_to_f`/`_delta_to_f`. | Two independent converters on one path means double conversion the moment both run. | **#231**: in °C mode the Thermostats form called `toStorage(16)` → 60.8, then the backend's `_to_f` converted again → **141.44 °F stored**. Both sides' unit tests passed because each asserted a *different* contract. Spawned the 3-file parity system (§2.1) and the dual-unit E2E matrix. Sibling °C bugs: #280, #281, #284, #291, #292. Full invariant owned by `plenum-architecture-contract`. |
| 3 | **Every backend/API knob must have a UI control** — a new `ThermostatConfig` field, system setting, or write-boundary tunable requires a form control + helper text on the matching React page, in the same change. 100% rule. | A knob reachable only via DB/API is an incomplete feature; the add-on's users operate through the ingress UI only. | Written rule in CLAUDE.md. No single triggering incident found in the mined issue history (checked closed-bugs.md / closed-features.md) — treat as a standing product decision, not folklore. |
| 4 | **Never mention Claude/AI authorship or include Claude session links** in commit messages, PR titles/bodies, or issue comments. | Repo owner's hygiene policy; keeps the public history tool-agnostic. | No incident in mined history; the full git log (450 commits checked) contains no such mention — the rule has held. |
| 5 | **After every push to a PR, update the PR body** (what changed, why, test plan) without being asked. **Review fixes go to the PR's own branch**, never a separate review branch. | Reviewers and the changelog generator (`release-pr.yml` builds release notes from PR titles) rely on PR metadata being current; fixes on a side branch never appear in the PR diff. | Written rule in CLAUDE.md; no specific incident found in mined history. |
| 6 | **Validate temperature bounds AFTER unit normalization** — convert to internal °F first, then apply the 40–90 °F range check, before persisting. | Bounds applied to the raw display value are meaningless when the unit varies: 150 raw could be °C or °F. | `.jules/sentinel.md` entry **2026-05-05 [MEDIUM]**: missing normalized-value validation allowed extreme setpoints through. See `_temp_range_error()` and `_validate_deadband_override()` in `routes.py` (converts via `_delta_to_f` *then* checks 0–10 °F) for the canonical pattern. |
| 7 | **Safety guards are one-way ratchets** — never weaken a protective interlock (short-cycle protection, cooling lockout, staleness guard, cycle timeout, min_open_vents, max_setpoint envelope) to fix a comfort complaint or a flaky test. **[INFERRED — not written in CLAUDE.md; derived from history, label it as inferred if you cite it in a PR.]** | These guards protect physical equipment (compressor slugging, dead-heading the air handler, runaway cycles). A regression is silent until hardware is damaged. | The **#208–#213 safety-hardening wave**: #208 no short-cycle protection; #209 no cold-weather compressor lockout (→ `cooling_lockout_below_f`); #210 `min_open_vents` bypass can dead-head the air handler; #211 no sensor-staleness guard; #212 cycle timeout had zero tests ("a protective interlock with no test is one that can silently regress"); #213 safety features shipped off by default. Plus #267 (thermostat unavailability suspended ALL safety monitoring) and #367/#368 (max_setpoint envelope not enforced with zero active rooms). |

---

## 2. Change classes and their gates

Identify which class(es) your change falls into. A change can be several at
once — clear every applicable gate.

### 2.1 New temperature field on a write boundary (highest-friction routine change)

Three files must change together; `smart_vent/backend/tests/test_temperature_field_parity.py`
fails CI if any is missed:

1. **Python registry**: `TEMPERATURE_FIELDS` dict in
   `smart_vent/backend/api/routes.py` (~line 243, 12 entries as of 2026-07,
   v0.22.1). Kind vocabulary (verified against the test's regex and the dict):
   `absolute` | `absolute_nullable` | `delta` | `delta_nullable`.
   (Note: the frontend `SAFETY_FIELDS` in `Thermostats.tsx` uses a *different*
   kind vocabulary — `absolute_temp`/`delta_temp`/`other` — for label
   rendering only. Don't confuse the two.)
2. **TypeScript manifest**: `TEMPERATURE_FIELDS` array in
   `e2e/tests/temperature-fields.ts` (`field`, `kind`, `ui`, `endpoints`).
3. **E2E round-trip**: a test in `e2e/tests/temperature-units.spec.ts` tagged
   `// @covers: <field>` (first non-whitespace on the line).

The parity test enforces five things (read the file — the assertions are the spec):
field sets identical; kinds agree (a kind mismatch means `_to_f` vs
`_delta_to_f` disagreement — silently corrupts by ±32 °F); every `ui: true`
entry has a `@covers` marker; no orphan `@covers` markers; every registered
field actually appears in `routes.py` source.

Also required:
- The handler must call `_to_f`/`_delta_to_f` (imported into `routes.py` from
  `backend/units.py` — CLAUDE.md says the helpers live in `routes.py`; they
  have since moved to `units.py` and are re-imported under the old names).
- Bounds check AFTER conversion (rule 6).
- A Celsius-mode backend integration test (set
  `client.app["scheduler"]._active_unit = "C"`, POST a °C value, assert stored °F).
- Frontend form: init via `toDisplay`/`toDisplayDelta`, submit the **raw
  display value** (rule 2), and a Celsius-mode vitest asserting the POST body
  carries the raw value (e.g. `min_setpoint: 16`, not 60.8).
- A UI control if this is a new knob (rule 3), which usually also triggers the
  golden-regeneration class (§2.3).

Why so heavy: the #231 class of bug is invisible to per-side unit tests by
construction. The round-trip matrix (°F and °C stacks in container-ci's
`Round-trip (F)` / `Round-trip (C)` jobs) is the only end-to-end guard.

### 2.2 New `config.yaml` option

- Add the key under `options:` in `smart_vent/config.yaml` **and** a
  `bashio::config '<key>'` or `get_config '<key>'` read + export in
  `smart_vent/run.sh`.
- Enforced by `smart_vent/backend/tests/test_addon_config.py`
  (`TestAddonConfigParity::test_every_option_is_read_by_run_sh`) — it parses
  the `options:` block with a regex (no PyYAML; PyYAML is deliberately NOT a
  dependency, do not `import yaml` anywhere).
- Rationale: without the `run.sh` read, the user's add-on setting silently has
  no effect at runtime (the docstring in the test states this class of bug).
- If the option is user-tunable, rule 3 applies (UI control) unless it is a
  pure deployment concern surfaced only through the HA add-on config panel.

### 2.3 Any rendered-UI change

- The visual-regression suite (the `E2E visual regression (F)/(C)` matrix jobs)
  screenshots every page against committed goldens in `e2e/screenshots/`
  (dual-unit filenames, e.g. `dashboard-Fahrenheit-chromium.png` /
  `dashboard-Celsius-chromium.png`, plus `-mobile` variants) and fails on any
  pixel deviation. Goldens for **both** unit sets must be regenerated.
- **Drift note (verified 2026-07):** CLAUDE.md still describes a standalone
  `.github/workflows/e2e.yml` with `max-parallel: 1` and in-job golden
  commits. That workflow no longer exists — the visual-regression matrix now
  lives in `.github/workflows/container-ci.yml` (jobs `e2e` +
  `commit-goldens`). Both unit legs run **in parallel**; each leg that
  regenerates goldens verifies them in a second pass, uploads only its own
  unit's PNGs as artifact `goldens-F`/`goldens-C`, and a fan-in
  `commit-goldens` job makes one combined commit + rebase + push to the PR
  branch (`ci: update E2E golden screenshots (F + C)`). The detached-HEAD push
  bug in the old fan-in was #369.
- Your obligations: review every changed PNG in the diff like code; if the
  change adds time-varying or engine-driven UI (clocks, timers, feeds, live
  counts), wrap it in `<Frozen>` from `frontend/src/ci.tsx` or goldens never
  stabilise (#182 campaign). Do NOT freeze static fixture values, and keep the
  single `isCI` branch inside `ci.tsx` (inline branching tanks branch
  coverage).

### 2.4 New API endpoint

- Every `/api/` route must carry `@docs` and a `@response_schema` documenting
  a 200 or 201 response. Enforced by
  `smart_vent/backend/tests/test_api_spec_enforcement.py`
  (checks `handler.__apispec__` exists and `responses` contains 200/201).
  Verified exceptions: `/api/backup` and `/api/metrics/export.csv`
  (response-schema check only; they still need `@docs`).
- The decorators are **documentation-only** — no validation middleware exists
  (#188 apispec migration). Your handler must parse and validate
  `await request.json()` itself.
- Exception handling per rule 1 (CWE-209 pattern).
- Temperature fields in the body → also class 2.1.

### 2.5 New dependency

- Runtime or test dep: add to `[project]` or
  `[project.optional-dependencies] dev` in `smart_vent/pyproject.toml`. All
  Python CI jobs install via `pip install ".[dev]"` — no workflow edit needed.
- Frontend: normal `package.json` flow.
- Inferred house norm (label as inferred): runtime dependencies are added
  reluctantly — the add-on is offline/ingress/CSP-constrained (the Swagger UI
  is self-hosted from `swagger-ui-bundle` for exactly this reason).
- PyYAML is explicitly banned (see §2.2).

### 2.6 Engine / safety-guard change — the highest bar

Files: `smart_vent/backend/engine/cycle_engine.py`, `vent_controller.py`,
`room_manager.py`, plus safety-relevant `ThermostatConfig` fields.

- Rule 7 (one-way ratchet, inferred) governs: you may tighten or add guards;
  weakening one requires an explicit, argued justification in the PR body —
  never do it as a side effect.
- Never convert units inside the engine — it receives pre-converted °F
  (CLAUDE.md key architectural fact).
- Evidence bar set by the safety wave: every protective behavior needs a test
  that exercises the *consequence*, not just the reason-string (#212's
  complaint); boundary tests on both sides of each threshold; defaults pinned
  by a test so a default change is "a deliberate, reviewed event" (#213);
  degraded-input behavior (stale/unavailable/absent sensor) defined and tested
  (#211, #267).
- Expect reviewers to ask for a live-log or simulated-tick trace demonstrating
  the changed behavior (#367 was diagnosed from live logs).

---

## 3. Coverage and lint gates (verified numbers — CLAUDE.md's are stale)

| Gate | Verified value (2026-07, v0.22.1) | Where | CLAUDE.md says |
|---|---|---|---|
| Backend coverage | `fail_under = 92.5` | `smart_vent/pyproject.toml` line 48 | 90 (stale) |
| Frontend coverage | lines 90, functions 85, branches 72, statements 87 | `smart_vent/frontend/vite.config.ts` lines 29–34 | 80.9 / 71.1 / 77.1 / 80.9 (stale) |
| Python lint | `ruff check backend/` + `ruff format --check backend/` | `lint.yml` | same |
| Types | `mypy backend/ --ignore-missing-imports` | `lint.yml` | same |
| Frontend lint | `npm run lint` + `npm run format:check` | `lint.yml` | same |
| Security | Trivy fs scan of source (lint.yml); Trivy image scan on release PRs (container-ci) — no CRITICAL allowed | `lint.yml`, `container-ci.yml` | same |

Practical implication of rising thresholds: coverage is ratcheted, not fixed.
Ship tests with every change; a change that lowers coverage below the ratchet
fails CI even if all tests pass.

Run locally before pushing (from repo root):

```bash
cd smart_vent && ruff format backend/ && ruff check backend/
cd smart_vent && python -m pytest backend/tests/ -v
cd smart_vent/frontend && npx vitest run --coverage
```

---

## 4. CI and release flow

### Per-PR pipeline
- `lint.yml` — the table above, on every PR/push to main.
- `container-ci.yml` — builds the image ONCE (#333/#337) and reuses it for the
  smoke test, the `Round-trip (F)/(C)` conversion matrix, and the
  `E2E visual regression (F)/(C)` matrix + `commit-goldens` fan-in. Build
  modes, decided at runtime by the `Build (PR validation)` job:
  - **Normal same-repo PR** → multi-arch, push throwaway `ghcr.io/<repo>:ci-<sha>`; version pinned to `CI` for deterministic screenshots.
  - **Release PR** (head branch `release/v*`, same-repo only — a fork branch named `release/v*` never gets the publish path) → multi-arch, push real `:<version>` + `:latest`, then Trivy image scan. Downstream jobs pull the explicit `:<version>` tag, never `:latest`.
  - **Fork PR** → single-arch, `docker save` artifact handoff (read-only tokens).
- `docker.yml` — push-to-main only, and only when `smart_vent/config.yaml`
  changed outside the release flow; release-PR merges are skipped (image
  already published during the PR).
- `ci-image-cleanup.yml` — nightly prune of `ci-*` tags; never `:latest` or semver tags.
- `validate-release.yml` — full dry-run (lint, both test suites, docker build,
  healthz smoke, °F visual regression); runs on `workflow_dispatch` and on
  `release/v*` PRs, but the bot-opened release PR won't auto-trigger it
  (GITHUB_TOKEN loop guard) — **start it with one click from the PR's checks**.

### Cutting a release (per `RELEASE.md`, verified against `release-pr.yml`)
1. Dry-run first: Actions → Validate Release → Run workflow; all green before tagging.
2. `git checkout main && git pull`, `git tag v0.X.Y`, `git push origin v0.X.Y`.
3. `release-pr.yml` fires: bumps version in **three files that must agree**
   (`smart_vent/config.yaml`, `smart_vent/pyproject.toml`,
   `smart_vent/frontend/package.json` + lockfile), prepends `CHANGELOG.md`
   (from merged-PR titles, release-housekeeping PRs filtered out), opens
   "Release vX.Y.Z" PR, populates the GitHub Release notes.
4. Merge gates: required check green, Trivy shows no CRITICAL, then merge — no
   second build runs (image was pushed during the PR).
5. If the Docker build fails during the release PR: **do not merge**; fix via
   a new feature PR, then delete the tag and cut the next patch version
   (runbook in `RELEASE.md` "If the Docker build fails during a release PR").

### Branch protection and policy facts
- Required check for release PRs is **`Build (PR validation)`** (container-ci)
  per CLAUDE.md, post-#337. **Drift note:** `RELEASE.md` still says the
  required check is "Build & Push release image" and attributes the release
  build to `docker.yml`'s `build-release` — both stale; CLAUDE.md and
  `container-ci.yml`'s own comments are authoritative. (Actual GitHub branch-
  protection settings not directly verifiable from the working tree —
  UNVERIFIED beyond these two documents.)
- One release per day maximum under normal circumstances.
- ALL changes via PR — no direct pushes to `main`, even hotfixes, even under
  pressure ("it bypasses CI and creates the exact reactive cycle we've had
  before" — RELEASE.md).
- Semver: fixes → patch, features → minor, breaking → major; no skipping patch versions.

---

## 5. Decision table: you are about to change X

| You are about to… | Gates you must clear | Evidence you must produce |
|---|---|---|
| Add/rename a temperature field on any write endpoint | `test_temperature_field_parity.py` (3-file lockstep); round-trip matrix (F+C); backend + frontend coverage ratchets | `_to_f`/`_delta_to_f` call with correct kind; post-conversion 40–90 °F bounds; Celsius integration test (stored °F asserted); frontend test asserting raw display value in POST body; `@covers:` tagged round-trip; UI control |
| Add a `config.yaml` option | `test_addon_config.py` | `bashio::config`/`get_config` read + export in `run.sh`; UI control if user-tunable; docs of the default |
| Change anything a page renders | Visual-regression matrix (F+C legs in container-ci) | Regenerated goldens for BOTH units reviewed PNG-by-PNG in the diff; `<Frozen>` wrap for any new volatile UI |
| Add an API endpoint | `test_api_spec_enforcement.py`; coverage ratchets | `@docs` + `@response_schema` (200/201); handler-side body validation; CWE-209-safe except blocks (`log.exception` + generic `error()`) |
| Add a Python dependency | CI installs `".[dev]"` — nothing else | Entry in `pyproject.toml` `[project]` or dev extras; justification if runtime; never PyYAML |
| Touch engine / a safety guard | Full backend suite at 92.5% ratchet; reviewer scrutiny at the repo's highest bar | Consequence-level tests (not reason-strings), boundary tests both sides, pinned defaults, degraded-sensor behavior tests; explicit PR-body justification for ANY weakening (one-way ratchet, inferred rule) |
| Change a UI form that submits temperatures | Parity test §2.1 items 2–3 if fields change; vitest coverage; round-trip matrix | No `toStorage`/`toStorageDelta` on outgoing payloads (#231); init via `toDisplay`/`toDisplayDelta`; delta fields use the delta helpers (no −32 corruption) |
| Push any commit to a PR | — | Updated PR body (what/why/test plan), every time; fixes pushed to the PR's own branch; zero AI-authorship mentions or session links |
| Cut a release | Green Validate Release dry-run; required check `Build (PR validation)`; Trivy no CRITICAL | Three version files agree; changelog section correct; one-click start of validate-release on the bot PR |

---

## Provenance and maintenance

All facts verified against the working tree on **2026-07-04, v0.22.1**
(`smart_vent/config.yaml` `version: "0.22.1"`). Incident details from GitHub
issues #182, #208–#213, #231, #237, #267, #277, #297–#305, #329–#337, #367–#369
and `.jules/sentinel.md`. Rules 3–5's lack of a triggering incident, rule 7's
one-way-ratchet status, and the "dependencies added reluctantly" norm are
**inferred**, and marked so above. Pytest could not be executed in the
authoring environment (no deps installed); test-behavior claims come from
reading the test sources.

Re-verify volatile facts:

```bash
# Coverage ratchets (backend / frontend)
grep -n fail_under smart_vent/pyproject.toml
grep -n -A5 thresholds smart_vent/frontend/vite.config.ts
# Temperature field registry size + kinds
grep -n -A20 "^TEMPERATURE_FIELDS" smart_vent/backend/api/routes.py
# Parity gates still exist and pass
cd smart_vent && python -m pytest backend/tests/test_temperature_field_parity.py backend/tests/test_addon_config.py backend/tests/test_api_spec_enforcement.py -q
# Visual-regression + fan-in still live in container-ci (e2e.yml stays deleted)
grep -n "commit-goldens\|E2E visual regression" .github/workflows/container-ci.yml; ls .github/workflows/
# Required-check naming drift (RELEASE.md vs CLAUDE.md)
grep -n "required check" RELEASE.md CLAUDE.md
# response-schema exceptions
grep -n "EXCEPTIONS" smart_vent/backend/tests/test_api_spec_enforcement.py
```

Drift already known at authoring time (update this skill when fixed upstream):
CLAUDE.md's coverage numbers (90 / 80.9-71.1-77.1) are stale; CLAUDE.md still
describes a standalone `e2e.yml` with `max-parallel: 1` (now merged into
`container-ci.yml` with a parallel fan-in); CLAUDE.md places `_to_f` helpers in
`routes.py` (now `backend/units.py`, re-imported); RELEASE.md's required-check
name and `docker.yml` attribution predate #337.
