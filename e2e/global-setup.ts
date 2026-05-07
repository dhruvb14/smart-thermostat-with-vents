/**
 * Playwright global setup — runs once before all tests.
 *
 * Configures the addon by navigating through the actual UI using a headless
 * Chromium browser. This means the setup exercises the same code paths a real
 * user would follow and verifies that the app can communicate with the Home
 * Assistant instance end-to-end.
 *
 * Flow:
 *   1. Wait for the addon to be healthy.
 *   2. Enable dev mode (REST — prevents the engine from issuing real HA
 *      commands, which keeps entity states stable during screenshot tests).
 *   3. Register two thermostat configs via the UI: the EntityPicker calls
 *      /api/ha/entities which proxies through to HA, so this verifies the
 *      HA connection is alive before any test runs.
 *   4. Create four rooms via the UI and wire up their sensors and vents with
 *      the EntityPicker (cover / sensor entities from HA).
 *   5. Add a weekday schedule for Living Room via the REST API (the schedule
 *      form requires many fields; REST is simpler and tests nothing HA-specific).
 *
 * Idempotent: if rooms already exist the whole setup is skipped, so re-runs
 * in the same environment work without tearing down and recreating everything.
 *
 * Requires a running HA instance with the fake entities defined in
 * e2e/fixtures/ha-config/configuration.yaml.  Without real HA the EntityPicker
 * returns no results and the setup will time out — run with Docker Compose:
 *   docker compose -f docker-compose.test.yml up --wait homeassistant
 */

import { chromium } from "@playwright/test";

const BASE_URL = process.env.PLENUM_URL ?? "http://localhost:8099";
const API = `${BASE_URL}/api`;

async function waitForAddon(): Promise<void> {
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${API}/healthz`);
      if (res.ok) return;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  throw new Error("Addon did not become healthy within 90s");
}

async function post(path: string, body: unknown): Promise<Response> {
  const res = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "(no body)");
    throw new Error(`POST ${API}${path} → ${res.status}: ${text}`);
  }
  return res;
}

const THERMOSTATS = [
  { entityId: "climate.downstairs_thermostat", name: "Downstairs Thermostat" },
  { entityId: "climate.upstairs_thermostat", name: "Upstairs Thermostat" },
] as const;

const ROOM_DEFS = [
  {
    name: "Living Room",
    thermostat: "climate.downstairs_thermostat",
    sensor: "sensor.living_room_temperature",
    vent: "cover.living_room_vent",
    addSchedule: true,
  },
  {
    name: "Bedroom",
    thermostat: "climate.upstairs_thermostat",
    sensor: "sensor.bedroom_temperature",
    vent: "cover.bedroom_vent",
    addSchedule: false,
  },
  {
    name: "Kitchen",
    thermostat: "climate.downstairs_thermostat",
    sensor: "sensor.kitchen_temperature",
    vent: "cover.kitchen_vent",
    addSchedule: false,
  },
  {
    name: "Office",
    thermostat: "climate.upstairs_thermostat",
    sensor: "sensor.office_temperature",
    vent: "cover.office_vent",
    addSchedule: false,
  },
] as const;

export default async function globalSetup(): Promise<void> {
  await waitForAddon();

  // Idempotent: skip if rooms already seeded
  const roomsRes = await fetch(`${API}/rooms`);
  const rooms: Array<{ id: string }> = await roomsRes.json();
  if (rooms.length > 0) {
    console.log("[e2e] Already configured — skipping setup");
    return;
  }

  // Enable dev mode — prevents the engine from issuing real HA service calls,
  // which keeps entity states (and therefore screenshots) stable.
  await post("/system/dev-mode", { dev_mode: true });

  console.log("[e2e] Configuring via UI (requires real HA)...");

  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH }
      : {}),
  });

  const page = await browser.newPage();
  // Allow more time per action — HA entity fetches can be slow on first request
  page.setDefaultTimeout(30_000);

  try {
    // ── Step 1: Register thermostats ─────────────────────────────────────────
    await page.goto(`${BASE_URL}/thermostats`);
    await page.waitForSelector(".loading", { state: "detached", timeout: 20_000 });
    await page.waitForLoadState("networkidle");

    for (const tc of THERMOSTATS) {
      await page.getByRole("button", { name: /Register thermostat/i }).click();
      await page.waitForSelector(".modal");

      // Type the short name to narrow the EntityPicker dropdown.
      // The EntityPicker filters by entity_id and friendly_name, so searching
      // "downstairs" matches both the entity ID and the HA-configured name.
      const search = tc.entityId.split(".")[1].split("_")[0]; // "downstairs"|"upstairs"
      await page.locator(".entity-picker input").fill(search);
      // Dropdown requires a live HA connection; this will time out without Docker
      await page.waitForSelector(".entity-dropdown", { timeout: 20_000 });
      await page.locator(".entity-option").filter({ hasText: tc.entityId }).click();

      await page.locator("#add-thermo-name").fill(tc.name);
      await page.getByRole("button", { name: "Register" }).click();
      await page.waitForSelector(".modal", { state: "detached" });
    }

    // ── Step 2: Create rooms and configure sensors / vents ───────────────────
    await page.goto(`${BASE_URL}/rooms`);
    await page.waitForSelector(".loading", { state: "detached", timeout: 20_000 });
    await page.waitForLoadState("networkidle");

    for (const def of ROOM_DEFS) {
      // Open the New Room modal
      await page.getByRole("button", { name: /Add room/i }).click();
      await page.waitForSelector(".modal");
      await page.locator("#room-name").fill(def.name);
      await page.locator("#room-thermostat").selectOption(def.thermostat);
      await page.getByRole("button", { name: /Create room/i }).click();
      // After creating a new room the app automatically navigates to the
      // RoomConfigure view for that room (see Rooms.tsx onSave handler).
      await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
      await page.waitForLoadState("networkidle");

      // Add temperature sensor — placeholder: "Search temperature sensors (sensor.*)…"
      const sensorSearch = def.sensor.split("_")[1]; // "living"|"bedroom"|etc.
      await page.locator('input[placeholder*="temperature sensor"]').fill(sensorSearch);
      await page.waitForSelector(".entity-dropdown", { timeout: 20_000 });
      await page.locator(".entity-option").filter({ hasText: def.sensor }).click();

      // Add vent — placeholder: "Search vents (cover.*)…"
      const ventSearch = def.vent.split("_")[1]; // "living"|"bedroom"|etc.
      await page.locator('input[placeholder*="vent"]').fill(ventSearch);
      await page.waitForSelector(".entity-dropdown", { timeout: 20_000 });
      await page.locator(".entity-option").filter({ hasText: def.vent }).click();

      // Navigate back to the rooms list for the next room
      await page.getByRole("button", { name: /← Back|Back/i }).click();
      await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
    }

    // ── Step 3: Add schedule for Living Room (REST — no HA interaction) ──────
    const updatedRooms: Array<{ id: string; name: string }> = await (
      await fetch(`${API}/rooms`)
    ).json();
    const livingRoom = updatedRooms.find((r) => r.name === "Living Room");
    if (livingRoom) {
      await post(`/rooms/${livingRoom.id}/schedules`, {
        days_of_week: [0, 1, 2, 3, 4], // Mon–Fri
        start_time: "08:00",
        end_time: "17:00",
        target_temp: 72.0,
      });
    }
  } finally {
    await browser.close();
  }

  console.log("[e2e] Setup complete.");
}
