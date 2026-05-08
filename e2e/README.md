# Plenum E2E visual regression tests

Playwright tests run against a real Home Assistant instance + the addon in Docker Compose.
Golden screenshots are stored in `e2e/screenshots/` and committed to git.
CI diffs every PR against the committed goldens and fails on any pixel deviation.

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
| Sensor (temp) | `sensor.living_room_temperature`, `sensor.bedroom_temperature`, `sensor.kitchen_temperature`, `sensor.office_temperature` |

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

> ⚠ Without Docker there is no HA instance. `global-setup.ts` will time out
> waiting for EntityPicker dropdown results (entity discovery requires HA).
> **Do not use this path to generate committed goldens** — use Docker instead.
> This path is useful only for smoke-testing the addon UI in isolation.

Docker is not available in all environments (e.g. Claude Code cloud sessions).
This path runs the addon backend directly with Python and downloads Chrome for Testing.

**Prerequisites:** Python 3.11+, Node 20+

```bash
# 1. Install backend Python dependencies
pip install aiohttp aiosqlite apscheduler python-dotenv aiohttp-apispec \
            marshmallow "marshmallow<4" marshmallow-dataclass websockets

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

# 4. Seed the addon with test data (rooms, vents, sensors, thermostats, schedule)
#    Run global-setup.ts logic manually, or call the REST API directly:
BASE=http://localhost:8099/api
curl -s -X POST $BASE/system/dev-mode \
     -H "Content-Type: application/json" -d '{"dev_mode":true}' > /dev/null
# ... then POST /api/rooms, /api/rooms/{id}/sensors, etc. as per global-setup.ts

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

- Runs on every PR targeting `main`.
- HA startup waits for the Docker healthcheck to pass (`docker compose up --wait`) before attempting token creation — this avoids the 1.5 GB image-pull race that caused earlier token failures.
- If any screenshot differs from the committed golden → CI fails.
- Diff images are uploaded as a `screenshot-diffs` artifact (14-day retention).
- To trigger a manual run: `Actions → E2E visual regression → Run workflow`.

---

## Tear down

```bash
docker compose -f docker-compose.test.yml down -v
```

The `-v` flag removes the named volumes (HA data and addon data).
Omit it if you want to keep the environment for iterative development.
