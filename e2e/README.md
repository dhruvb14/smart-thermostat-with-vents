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

e2e/global-setup.ts  ─ seeds the addon with rooms / vents / schedules via REST
e2e/tests/*.spec.ts  ─ navigates each page, compares against screenshots/
```

Fake entity IDs (defined in `e2e/fixtures/ha-config/configuration.yaml`):

| Type | Entity IDs |
|---|---|
| Climate (thermostat) | `climate.downstairs_thermostat`, `climate.upstairs_thermostat` |
| Cover (vent) | `cover.living_room_vent`, `cover.bedroom_vent`, `cover.kitchen_vent`, `cover.office_vent` |
| Sensor (temp) | `sensor.living_room_temperature`, `sensor.bedroom_temperature`, `sensor.kitchen_temperature`, `sensor.office_temperature` |

---

## Running locally (first time — generating golden screenshots)

**Prerequisites:** Docker, Node 20+, Python 3.9+

```bash
# 1. Start Home Assistant
docker compose -f docker-compose.test.yml up -d homeassistant

# 2. Create HA admin user + token (polls until HA is healthy, ~60s)
pip install requests
python3 e2e/scripts/setup-ha.py \
  --ha-url http://localhost:8123 \
  --output /tmp/ha_token.txt

# 3. Start the addon with the token
HA_TOKEN=$(cat /tmp/ha_token.txt) \
docker compose -f docker-compose.test.yml up -d plenum

# 4. Install Playwright
cd e2e && npm ci && npx playwright install chromium

# 5. Generate golden screenshots (first run only)
npm run test:update   # runs `playwright test --update-snapshots`

# 6. Review the new screenshots in e2e/screenshots/, then commit them
git add e2e/screenshots/
git commit -m "add E2E golden screenshots"
```

---

## Updating goldens after an intentional UI change

1. Make your UI changes in a branch.
2. Start the test environment (steps 1–4 above).
3. Run `cd e2e && npm run test:update`.
4. Open `e2e/screenshots/` and visually inspect each updated PNG.
5. If everything looks correct, commit the updated PNGs to the same PR as your UI change.
6. In the PR, reviewers will see the image diffs in the GitHub file diff view — review them like any other code change.
7. Approve and merge once the visual changes look intentional and correct.

---

## CI behaviour

- Runs on every PR targeting `main`.
- If any screenshot differs from the committed golden → CI fails.
- Diff images (side-by-side expected vs. actual) are uploaded as a `screenshot-diffs` artifact (14-day retention) so failures are easy to diagnose without a local repro.
- To trigger a manual run (e.g. to verify goldens match a fresh environment): `Actions → E2E visual regression → Run workflow`.

---

## Tear down

```bash
docker compose -f docker-compose.test.yml down -v
```

The `-v` flag removes the named volumes (HA data and addon data).
Omit it if you want to keep the environment for iterative development.
