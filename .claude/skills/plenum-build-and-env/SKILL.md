---
name: plenum-build-and-env
description: >-
  Recreate the Plenum (smart-thermostat-with-vents) development environment
  from scratch: prerequisites, backend pip install, frontend npm install/build,
  .env setup, VS Code launch configs, Docker image build, docker-compose test
  stack anatomy, and local E2E/Playwright setup. Load when setting up a fresh
  clone, when an install/build/test command fails with missing deps or wrong
  versions, when Vite/dev-server ports or proxies misbehave, or when you need
  to know where a build artifact or the SQLite DB lives. Do NOT load for
  actually running/operating the app (plenum-run-and-operate) or for CI
  workflow internals (plenum-ci-and-release).
---

# Plenum build & environment — from bare machine to green tests

Everything needed to stand up a dev environment for this repo and the traps
that waste time. All commands verified against the repo at v0.22.1 unless
marked **stated-not-executed** or **UNVERIFIED**.

**When NOT to use this skill:**
- Starting/operating the app, data locations at runtime, backup/restore,
  ingress/MCP ports → `plenum-run-and-operate`.
- CI workflow mechanics (container-ci build modes, golden auto-commit) →
  `plenum-ci-and-release`.
- What gates a change must clear before merging → `plenum-change-control`.
- Test-writing patterns, coverage gates in depth → `plenum-validation-and-qa`.

**Jargon** (defined once): *editable install* — `pip install -e`, links the
package to the source tree so edits apply without reinstall; *golden* — a
committed reference screenshot PNG under `e2e/screenshots/` compared
pixel-by-pixel by the visual-regression suite; *ingress* — Home Assistant's
built-in reverse proxy that serves add-on UIs inside the HA frontend.

---

## 1. Prerequisites

| Tool | Version | Source of truth |
|---|---|---|
| Python | **>= 3.12** (hard requirement) | `smart_vent/pyproject.toml` `requires-python = ">=3.12"` |
| Node.js | **20** is what CI pins; no `engines` field exists in `package.json`, so newer works (verified: installs and tests pass on Node 22) | `.github/workflows/lint.yml` / `container-ci.yml` / `validate-release.yml` all use `setup-node` with `node-version: "20"` |
| npm | ships with Node; lockfiles are `package-lock.json` (npm, not yarn/pnpm) | `smart_vent/frontend/package-lock.json`, `e2e/package-lock.json` |
| Docker + Compose v2 | needed only for the container build and E2E stack | `docker-compose.test.yml` |

Trap: `python3` on a stock machine may be 3.10/3.11. pip will refuse the
install (`requires-python >=3.12`). Use `python3.12 -m venv ...` explicitly.

---

## 2. Backend install

Two equivalent commands (both verified against `pyproject.toml`):

```bash
# From the repo root — editable install WITH dev/test extras (what you want):
python3.12 -m venv .venv
.venv/bin/pip install -e './smart_vent[dev]'

# What CI does (cwd = smart_vent/, non-editable):
pip install ".[dev]"
```

- `.venv/` and `*.egg-info/` are gitignored (root `.gitignore`), so an
  in-repo venv does not dirty the tree. `.vscode/settings.json` expects the
  interpreter at `${workspaceFolder}/.venv/bin/python`.
- The README's minimal path (`pip install -e ./smart_vent`, no extras) runs
  the app but cannot run pytest/ruff/mypy — always add `[dev]` for
  development.
- The VS Code task "backend: install (.venv)" (`.vscode/tasks.json`) runs
  exactly the repo-root command above.

Runtime deps live in `[project] dependencies`; test/lint deps in
`[project.optional-dependencies] dev` (pytest, pytest-asyncio, pytest-cov,
aioresponses, ruff, mypy, types-python-dateutil). **New deps of either kind go
in `pyproject.toml`** — CI installs via `pip install ".[dev]"`, no workflow
edit needed. **PyYAML is NOT a dependency** — never `import yaml` in code or
tests (config.yaml parsing in tests is done without it).

Smoke test (verified in this environment — 30 tests pass in <1 s):

```bash
cd smart_vent
python -m pytest backend/tests/test_units.py backend/tests/test_addon_config.py \
    backend/tests/test_temperature_field_parity.py -q --no-cov
```

- `--no-cov` matters for subsets: `pyproject.toml` sets
  `addopts = "--cov=backend --cov-report=term-missing"` and
  `fail_under = 93.9` (as of 2026-07; CLAUDE.md corrected 2026-07-05 —
  the repo file always wins), so a partial run without `--no-cov` fails the coverage
  gate even when every test passes.
- Full suite: `python -m pytest backend/tests/ -v` from `smart_vent/`.
- History note: `tests/test_routes_helpers.py` (named in pre-2026-07-05
  CLAUDE.md copies) no longer exists; the conversion-helper unit tests are
  `backend/tests/test_units.py`, matching the helpers' move from
  `routes.py` privates to `backend/units.py` (`to_f`, `delta_to_f`, `from_f`,
  `from_f_delta`; `routes.py` imports them under the old `_to_f`-style
  aliases). See `plenum-architecture-contract` for the conversion contract.

Lint/format/typecheck (run before committing Python; see
`plenum-change-control` for gating):

```bash
cd smart_vent
ruff check backend/ && ruff format backend/    # format --check is what CI runs
mypy backend/ --ignore-missing-imports
```

---

## 3. Frontend install & build

```bash
cd smart_vent/frontend
npm ci            # reproducible, from package-lock.json (CI uses this)
# npm install     # only when intentionally changing dependencies
npm run build     # runs `tsc && vite build` → dist/  (dist/ is gitignored)
```

Verified script names from `smart_vent/frontend/package.json` — use these
exact names, several are commonly misguessed:

| Script | Command |
|---|---|
| `npm run dev` | `vite` (dev server) |
| `npm run build` | `tsc && vite build` |
| `npm run lint` / `lint:fix` | `eslint src/` |
| `npm run format` / `format:check` | `prettier --write src/` / `--check src/` |
| `npm test` / `test:run` | `vitest` (watch) / `vitest run` |
| `npm run test:coverage` | `vitest run --coverage` |

Smoke test (verified: `npx vitest run src/contexts.test.ts` — 13 tests pass).
Coverage thresholds live in `vite.config.ts` `test.coverage.thresholds`
(four ratcheted values, recalibrated for Vitest 4's v8 remapping; threshold
table of record: `plenum-validation-and-qa` §6).

### Vite dev server: port and proxies

`vite.config.ts` sets **no `server.port`**, so the dev server uses Vite's
default **5173**, proxying `/api` → `http://localhost:8099` and `/ws` →
`ws://localhost:8099` (WebSocket) to the locally running backend. README and
`.vscode/launch.json` both say 5173. The "5174" in issue #25 comes from a
*draft* CONTRIBUTING.md inside the issue body, not from repo config —
5174 only ever appears because Vite auto-increments when 5173 is already
bound (inference). Trust `vite.config.ts`.

`VITE_APP_VERSION` is a build-time constant. The Docker build extracts it
from `config.yaml`'s `version:` and bakes it into the bundle; the value
`"CI"` flips the frontend into frozen/deterministic mode via
`frontend/src/ci.tsx` (see `plenum-ci-and-release` for why). For local dev
builds you normally leave it unset.

---

## 4. `.env` setup

`cp .env.sample .env` at the repo root, then edit. The sample's exact
contents (all four variables):

```bash
HA_URL=http://homeassistant.local:8123
HA_TOKEN=your_long_lived_token_here
DATA_DIR=./data
PORT=8099
```

- `HA_TOKEN`: HA profile → Long-Lived Access Tokens → Create Token.
- The backend also honours `TEMPERATURE_UNIT` (`F`/`C`/empty=auto-detect),
  `MCP_PORT` (default 9099), `TZ`, `HA_USE_WSS`, `HA_SSL_VERIFY` — full knob
  inventory belongs to `plenum-config-and-flags`.
- `.env` and `data/` are gitignored. `python-dotenv` is a runtime dep, and
  the VS Code backend launch loads `.env` via `envFile`.

### DATA_DIR defaults differ by context — the volume-loss trap

| Context | Default | Where set |
|---|---|---|
| Bare `python -m backend.main` | `/data` | `backend/main.py` line 42 (`os.environ.get("DATA_DIR", "/data")`) |
| Local dev via `.env.sample` | `./data` | `.env.sample` |
| Add-on (`run.sh`) | `/config` | `run.sh` `export DATA_DIR="${DATA_DIR:-/config}"` — with a one-time `flair.db`/`app.db` copy-migration from legacy `/data`, because `/config` is the Samba-visible add-on config share |
| Compose test stack | `/data` on named volume `plenum-data` | `docker-compose.test.yml` |

The DB is `${DATA_DIR}/app.db` (SQLite, WAL sidecars `-wal`/`-shm`). Traps:
running the backend locally **without** setting `DATA_DIR` tries to write to
`/data` (usually fails or lands in an unexpected root dir); and
`docker compose ... down -v` **deletes the named volumes** — HA data and the
Plenum DB — so omit `-v` to keep state between E2E iterations. Runtime data
management/backup is `plenum-run-and-operate`'s territory.

---

## 5. VS Code integration

`.vscode/launch.json` configurations (enumerated, verified):

| Name | Type | What it does |
|---|---|---|
| Backend (Python) | debugpy | `python -m backend.main`, cwd `smart_vent/`, loads repo-root `.env`, sets `DEV_DOCS=1` (note: `DEV_DOCS` is read nowhere in the codebase as of v0.22.1 — vestigial, harmless) |
| Backend tests (pytest) | debugpy | `pytest backend/tests/ -v` under the debugger, cwd `smart_vent/` |
| Frontend (Chrome) | chrome | opens `http://localhost:5173`, pre-launch task starts `npm run dev` |
| Frontend (Edge) | msedge | same, Edge |
| **Compound:** Full stack (Backend + Frontend) | — | Backend (Python) + Frontend (Chrome), `stopAll: true` |

`.vscode/tasks.json` tasks: `frontend: dev server` (background, dependsOn
install), `frontend: install` (`npm install`), `frontend: build`,
`backend: install (.venv)`, `backend: test`, `backend: ruff`.
`.vscode/settings.json`: pytest wired to `smart_vent/backend/tests`, Ruff as
Python formatter with organize-imports on save, Prettier for TS/JS/JSON,
ESLint working dir `smart_vent/frontend`. Recommended extensions in
`.vscode/extensions.json`: ms-python.python, ms-python.debugpy,
charliermarsh.ruff, dbaeumer.vscode-eslint, esbenp.prettier-vscode,
ms-vscode.vscode-typescript-next.

---

## 6. Docker image build (`smart_vent/Dockerfile`)

**Single-stage** build (no `ARG BUILD_FROM`, no multi-stage — despite what
older docs/habits from HA add-on templates suggest), `FROM
ghcr.io/home-assistant/base-python:latest` (multi-arch amd64+arm64 manifest;
includes bashio, s6-overlay, python3, pip, jq). Sequence:

1. `apk upgrade` + `apk add nodejs npm sqlite`.
2. `pip install --upgrade pip`, then copy `pyproject.toml` alone (with a stub
   `backend/__init__.py`) and `pip3 install .` — layer-cached so dependency
   installs survive source edits.
3. Copy `backend/`, `config.yaml`, then `frontend/package*.json` →
   `npm ci` → copy `frontend/` → `VITE_APP_VERSION=$(grep '^version:'
   config.yaml ...) npm run build` → `rm -rf node_modules` (build-time only;
   removed so esbuild's bundled binary doesn't trip image CVE scans).
4. Copy `run.sh` (entrypoint), `EXPOSE 8099`, `CMD ["/run.sh"]`.

Local build: `docker build -t plenum-e2e ./smart_vent` (stated-not-executed
in this environment — no Docker daemon; this is the compose `build:` context
and the tag compose defaults to). Version bump trap: the image bakes the
frontend version from `config.yaml`, so a version bump without a rebuild
shows the old version in the UI footer.

## 7. Compose test stack anatomy

`docker-compose.test.yml` (repo root) — three services, two named volumes
(`ha-data`, `plenum-data`):

- **ha-init** — alpine one-shot; copies
  `e2e/fixtures/ha-config/configuration.yaml` into the HA volume before HA
  starts.
- **homeassistant** — pinned `ghcr.io/home-assistant/home-assistant:2025.5.3`,
  port 8123, healthcheck curls `/api/config` (401 counts as healthy),
  `start_period: 90s` for slow CI pulls.
- **plenum** — `image: "${PLENUM_IMAGE:-plenum-e2e}"` with
  `build: context: ./smart_vent` as fallback. **`PLENUM_IMAGE`** is how CI
  injects the once-built `ghcr.io/<repo>:ci-<sha>` image so E2E legs don't
  rebuild (#333); unset locally, compose uses/builds local `plenum-e2e`.
  Env: `HA_URL=http://homeassistant:8123`, `HA_TOKEN` from the shell
  (produced by `e2e/scripts/setup-ha.py`), `TEMPERATURE_UNIT=F`,
  `DATA_DIR=/data`; port 8099; healthcheck `/api/healthz`.

`docker-compose.test.celsius.yml` is a **layered override**, not standalone —
its entire body overrides one value, `TEMPERATURE_UNIT: "C"` on the plenum
service:

```bash
docker compose -f docker-compose.test.yml -f docker-compose.test.celsius.yml up
```

HA itself stays °F either way — the fixture pins
`unit_system: us_customary` (the `158°F` bug root cause: unpinned YAML HA
defaults to metric, so `target_temp: 70` was read as 70 °C).

## 8. Local E2E environment (`e2e/`)

- `e2e/package.json`: only two scripts — `npm test` (`playwright test`) and
  `npm run test:update` (`playwright test --update-snapshots`). Sole dep:
  `@playwright/test`.
- `playwright.config.ts`: `baseURL` = `PLENUM_URL` env or
  `http://localhost:8099`; `workers: 1`; projects `chromium` + `mobile`
  (iPhone 14 viewport but forced `browserName: "chromium"` — WebKit is not
  installed in CI); `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` overrides the
  browser binary; snapshot names encode the unit via `PLENUM_TEMP_UNIT`
  (`dashboard-Fahrenheit-chromium.png` vs `-Celsius-`); global default
  `maxDiffPixels: 100`.
- `e2e/global-setup.ts` seeds the addon **through the UI** (EntityPicker
  needs a live HA behind `/api/ha/entities` — without Docker/HA it times
  out).
- `e2e/scripts/setup-ha.py` creates the HA admin user + long-lived token on
  the fresh fixture instance (`--ha-url ... --output /tmp/ha_token.txt`);
  needs `pip install requests websocket-client` (deliberately NOT in
  pyproject — they are fixture-tooling, not project deps).
- Fixture entities (2 climate, 4 cover, 4 sensor) are listed in
  `e2e/README.md`, which is the full local-run runbook (Docker path and a
  degraded no-Docker path). Docker-stack bring-up order: `up --wait
  homeassistant` → `setup-ha.py` → `HA_TOKEN=... up -d plenum` → `cd e2e &&
  npm ci && npx playwright install chromium` → `npm test` (or `test:update`
  to regenerate goldens — review PNG diffs like code; see
  `plenum-change-control`). Stated-not-executed here (no Docker daemon).
  Caveat: `e2e/README.md`'s no-Docker section still names the abandoned
  `aiohttp-apispec` in its pip list — prefer `pip install -e
  './smart_vent[dev]'` over that hand-rolled list.

---

## 9. Known traps (checklist)

- [ ] Python < 3.12 → pip refuses the install. Use `python3.12` explicitly.
- [ ] **PyYAML is not a dependency.** Do not `import yaml` anywhere.
- [ ] New Python dep (runtime or test) → `pyproject.toml` only
      (`[project]` deps or `dev` extra). CI picks it up automatically.
- [ ] Pytest subset without `--no-cov` → coverage gate (93.9%) fails a
      passing subset because `addopts` always enables `--cov`.
- [ ] `ruff format backend/` before committing Python — CI checks formatting
      (`ruff format --check`) separately from linting (`ruff check`).
- [ ] Frontend script names: it is `npm run format:check` and
      `npm run test:coverage` — not `format-check`/`prettier:check`/etc.
- [ ] `npm ci`, not `npm install`, for reproducible installs (CI and
      Dockerfile both use `ci`).
- [ ] Vite dev server is 5173 (no port override in `vite.config.ts`); if you
      see 5174, something else already holds 5173.
- [ ] `DATA_DIR` unset when running the backend bare → tries `/data`. Set it
      (`.env` uses `./data`).
- [ ] `docker compose ... down -v` wipes the `plenum-data` and `ha-data`
      volumes (DB gone). Omit `-v` to keep state.
- [ ] Gitignore already covers `.venv/`, `venv/`, `node_modules/`, `dist/`,
      `data/`, `*.db*`, `.coverage`, `htmlcov/`, frontend `coverage/` — a
      normal dev setup leaves `git status` clean.
- [ ] Where CLAUDE.md prose and repo files disagree (coverage numbers, helper
      locations, test filenames), **the repo files win** — verify before
      quoting CLAUDE.md figures.

---

## Provenance and maintenance

Facts verified 2026-07-04 against v0.22.1 (HEAD `c65d35d`). Executed in a
live container here: Python 3.12 venv + `pip install -e './smart_vent[dev]'`;
pytest subset (`test_units.py`, `test_addon_config.py`,
`test_temperature_field_parity.py` — 30 passed); `npm ci` in
`smart_vent/frontend` (280 packages, Node 22) and
`npx vitest run src/contexts.test.ts` (13 passed). NOT executed (no Docker
daemon): `docker build`, compose stack, Playwright runs — commands
transcribed from `docker-compose.test.yml` / `e2e/README.md`.

Re-verification one-liners for volatile facts:

| Fact | Command |
|---|---|
| requires-python, dep lists, coverage `fail_under` | `grep -A2 fail_under smart_vent/pyproject.toml; grep requires-python smart_vent/pyproject.toml` |
| CI Node version | `grep -rn node-version .github/workflows/` |
| Frontend script names & coverage thresholds | `cat smart_vent/frontend/package.json; grep -A6 thresholds smart_vent/frontend/vite.config.ts` |
| Vite port/proxy | `cat smart_vent/frontend/vite.config.ts` (no `server.port` ⇒ 5173) |
| `.env.sample` variables | `cat .env.sample` |
| Launch configs / tasks | `cat .vscode/launch.json .vscode/tasks.json` |
| DATA_DIR defaults | `grep -n DATA_DIR smart_vent/backend/main.py smart_vent/run.sh docker-compose.test.yml` |
| Dockerfile shape | `cat smart_vent/Dockerfile` (still single-stage? still `base-python:latest`?) |
| PLENUM_IMAGE plumbing | `grep -n PLENUM_IMAGE docker-compose.test.yml .github/workflows/container-ci.yml` |
| HA fixture unit pin & image tag | `grep -n unit_system e2e/fixtures/ha-config/configuration.yaml; grep -n home-assistant: docker-compose.test.yml` |
| Conversion helpers location | `grep -n "def " smart_vent/backend/units.py` |
| PyYAML still absent | `grep -i yaml smart_vent/pyproject.toml` (expect no PyYAML) |
