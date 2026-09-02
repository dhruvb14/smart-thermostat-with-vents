# Plenum E2E visual regression tests

Playwright tests run against a real Home Assistant instance + the addon in Docker Compose.
Golden screenshots are stored in `e2e/screenshots/` and committed to git.
CI diffs every PR against the committed goldens and fails on any pixel deviation.

**Dual-unit goldens.** The suite runs once per display unit so UI temperature-conversion
regressions are caught in both directions. The `e2e` job in
`.github/workflows/container-ci.yml` matrixes over `[F, C]` (the °C leg layers
`docker-compose.test.celsius.yml`, setting `TEMPERATURE_UNIT=C` on Plenum), and the golden
filename encodes the unit via `playwright.config.ts`'s `PLENUM_TEMP_UNIT`-driven
`snapshotPathTemplate` — e.g. `dashboard-Fahrenheit-chromium.png` vs
`dashboard-Celsius-chromium.png`. Note the underlying Home Assistant fixture is pinned to
`unit_system: us_customary`, so HA itself is always °F; the matrix varies only the *addon's*
display unit.

**Theme/device axis.** `playwright.config.ts` also runs four projects —
`chromium`, `mobile`, `chromium-dark`, `mobile-dark` — so every spec produces goldens for
light and dark (`prefers-color-scheme` emulation, #458) on both desktop and mobile viewports;
the project name is the last segment of the filename (e.g.
`dashboard-Fahrenheit-chromium-dark.png`).

**Auth leg.** A separate `e2e-auth` job (`E2E visual regression (auth)`) runs only the
`@auth`-tagged specs (`auth.spec.ts`) against `docker-compose.test.auth.yml`
(`require_auth: true`, no HA Supervisor in front). It authenticates by injecting a signed
session cookie (`e2e/auth-cookie.ts`) rather than logging in through a real Supervisor, and
owns the `login-*`, `settings-menu-auth-*`, and `mcp-tokens-card-*` goldens — the F/C legs
above grep those specs out.

---

## How it works

```
docker-compose.test.yml
  ha-init     ─── copies configuration.yaml into the HA volume
  homeassistant ─ real HA with fake entities (thermostats, vents, sensors)
  plenum      ─── the addon, connected to HA via HA_TOKEN

e2e/global-setup.ts  ─ configures the addon by clicking through the UI in a
                       headless Chromium browser (EntityPicker → real HA entities)
e2e/tests/*.spec.ts  ─ navigates each page, compares against screenshots/
```

### Why UI-based setup?

`global-setup.ts` uses Playwright to navigate to `/thermostats`, open the
"Register Thermostat" modal, and pick entities from the **EntityPicker** — the
same flow a real user follows.  This verifies that the HA→Plenum connection is
alive before any test screenshot is taken.  Rooms are then created and their
sensors/vents wired up the same way.

Schedules are still added via the REST API (no HA entity selection needed there).

> **Docker required** — the EntityPicker calls `/api/ha/entities` which proxies
> to HA.  Without a running HA instance the dropdown is empty and the setup step
> times out.  Always run goldens against the Docker Compose stack (see below).

Fake entity IDs (defined in `e2e/fixtures/ha-config/configuration.yaml`):

| Type | Entity IDs |
|---|---|
| Climate (thermostat) | `climate.downstairs_thermostat`, `climate.upstairs_thermostat` |
| Cover (vent) | `cover.living_room_vent`, `cover.bedroom_vent`, `cover.kitchen_vent`, `cover.office_vent` |
| Sensor (temp) | `sensor.living_room_temperature`, `sensor.bedroom_temperature`, `sensor.kitchen_temperature`, `sensor.office_temperature`, `sensor.outdoor_temperature` |
| Binary sensor (presence) | `binary_sensor.office_occupancy` (held `on`, drives the Office room's presence-active golden) |

---

## Running locally with Docker (standard path — **required for golden generation**)

**Prerequisites:** Docker, Node 20+, Python 3.9+

> Golden screenshots **must** be generated against this Docker stack so they
> include real entity state (temperatures, vent positions).  Screenshots
> generated without Docker show "—" for all live values and will fail CI.

```bash
# 1. Start Home Assistant and wait for it to be healthy (~60–90 s)
docker compose -f docker-compose.test.yml up --wait homeassistant

# 2. Create HA admin user + long-lived token
pip install requests websocket-client
python3 e2e/scripts/setup-ha.py \
  --ha-url http://localhost:8123 \
  --output /tmp/ha_token.txt

# 3. Start the addon with the token
HA_TOKEN=$(cat /tmp/ha_token.txt) \
docker compose -f docker-compose.test.yml up -d plenum

# 4. Install Playwright
cd e2e && npm ci && npx playwright install chromium

# 5. Generate golden screenshots (first run only, or after intentional UI changes)
npm run test:update   # runs `playwright test --update-snapshots`

# 6. Review the new screenshots in e2e/screenshots/, then commit them
git add e2e/screenshots/
git commit -m "add E2E golden screenshots"
```

---

## Running without Docker (limited — no real HA)

> ⚠ Without Docker there is no HA instance. `global-setup.ts` detects this
> (`GET /api/ha/entities?domain=climate` returns nothing) and falls back to
> seeding rooms/thermostats/sensors via the REST API instead of driving the
> EntityPicker through the UI — no manual seeding or timeout to work around.
> **Do not use this path to generate committed goldens** — use Docker instead,
> since the fake entity IDs resolve to no real HA state and every room card
> renders "—" (see the note below).
> This path is useful only for smoke-testing the addon UI in isolation.

Docker is not available in all environments (e.g. Claude Code cloud sessions).
This path runs the addon backend directly with Python and downloads Chrome for Testing.

**Prerequisites:** Python 3.12+ (matches `smart_vent/pyproject.toml`'s `requires-python`), Node 20+

```bash
# 1. Install backend Python dependencies (runtime deps from smart_vent/pyproject.toml —
#    installing the package itself keeps this in sync as deps change)
pip install ./smart_vent

# 2. Build the frontend
cd smart_vent/frontend
VITE_APP_VERSION=e2e-test npm ci && npm run build
cd ../..

# 3. Start the backend (no real HA — it retries the WS connection but the API works)
mkdir -p /tmp/plenum-e2e-data
cd smart_vent
nohup env HA_URL="http://127.0.0.1:19999" HA_TOKEN="fake-token" \
          TEMPERATURE_UNIT="F" DATA_DIR="/tmp/plenum-e2e-data" PORT=8099 \
          python3 -m backend.main > /tmp/plenum-backend.log 2>&1 &
cd ..
# Wait for it to start
until curl -sf http://localhost:8099/api/healthz -o /dev/null; do sleep 2; done

# 4. No manual seeding needed: Playwright's globalSetup (e2e/global-setup.ts)
#    runs automatically before the test suite and, on detecting no reachable
#    HA, seeds rooms/vents/sensors/thermostats/a schedule via the REST API
#    itself (setupViaREST()). This step is a no-op — it stays only to show
#    the addon is healthy enough for the suite to talk to it.

# 5. Download Chrome for Testing (replaces the Playwright bundled browser)
CHROME_URL="https://storage.googleapis.com/chrome-for-testing-public/131.0.6778.87/linux64/chrome-linux64.zip"
wget -q "$CHROME_URL" -O /tmp/chrome.zip
cd /tmp && unzip -q chrome.zip

# Install shared library deps (Ubuntu/Debian)
apt-get install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
                   libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
                   libxrandr2 libgbm1 libasound2t64 2>/dev/null || true

# 6. Generate golden screenshots
cd /path/to/repo/e2e
npm ci
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/tmp/chrome-linux64/chrome \
PLENUM_URL=http://localhost:8099 \
npm run test:update

# 7. Review screenshots in e2e/screenshots/, commit them
git add e2e/screenshots/
git commit -m "add E2E golden screenshots"
```

> **Note on entity states**: Without a real HA instance, the addon's entity-state
> endpoints return empty data. Dashboard room cards will show "—" for temperatures
> and vent positions. The golden screenshots captured this empty-state UI accurately.
> When updating goldens from a full Docker environment, the screenshots will show live
> entity data instead — that's expected and correct.

---

## Updating goldens after an intentional UI change

1. Make your UI changes in a branch.
2. Start the test environment (Docker path above, steps 1–4).
3. Run `cd e2e && npm run test:update`.
4. Open `e2e/screenshots/` and visually inspect each updated PNG.
5. If everything looks correct, commit the updated PNGs to the same PR as your UI change.
6. In the PR, reviewers will see the image diffs in the GitHub file diff view — review them like any other code change.
7. Approve and merge once the visual changes look intentional and correct.

---

## CI behaviour

- Runs as the `E2E visual regression (F)` / `(C)` / `(auth)` jobs inside
  `.github/workflows/container-ci.yml` (there is no standalone `e2e.yml`) on every PR targeting
  `main` whose diff touches a UI-affecting path — the `changes` job gates it, so a purely
  backend or docs-only PR skips these jobs (they report "skipped", which satisfies branch
  protection).
- HA startup waits for the Docker healthcheck to pass (`docker compose up --wait`) before attempting token creation — this avoids the 1.5 GB image-pull race that caused earlier token failures.
- If a screenshot differs from the committed golden, CI does not just fail: it re-runs with
  `--update-snapshots`, verifies the regenerated goldens actually pass, and (only if that
  succeeds) uploads them as a `goldens-F` / `goldens-C` / `goldens-auth` artifact. A fan-in
  `Commit updated goldens` job downloads those and pushes a single `ci: update E2E golden
  screenshots` commit back onto the PR branch — review the changed PNGs in that commit like
  any other code change; a green run does not mean nothing rendered differently.
- Test results (including Playwright's own diff/actual/expected images) are uploaded only when
  a run fails for a non-screenshot reason, as `playwright-results-F` / `-C` / `-auth`
  (14-day retention).
- To trigger a manual run: `Actions → Container CI → Run workflow` (this runs the whole
  pipeline — build, smoke test, conversion round-trip, and both visual-regression legs — not
  just the visual-regression jobs alone).

---

## Tear down

```bash
docker compose -f docker-compose.test.yml down -v
```

The `-v` flag removes the named volumes (HA data and addon data).
Omit it if you want to keep the environment for iterative development.
