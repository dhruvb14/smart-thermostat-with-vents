/**
 * Playwright global setup — runs once before all tests.
 *
 * Two paths depending on whether a real Home Assistant instance is reachable:
 *
 * A) WITH real HA (CI / Docker Compose):
 *    Configures the addon by navigating the actual UI with a headless Chromium
 *    browser.  The EntityPicker calls /api/ha/entities which proxies to HA, so
 *    this verifies the HA→Plenum connection is alive before any test screenshot
 *    is taken.
 *
 * B) WITHOUT real HA (Claude Cloud / local no-Docker):
 *    Falls back to REST API seeding.  The EntityPicker is mocked inside each
 *    individual spec that needs it, so the UI still renders realistically.
 *    This path must not be used to generate committed golden screenshots.
 *
 * Detection: attempts GET /api/ha/entities?domain=climate.  If the response
 * contains at least one entity, HA is reachable (path A).  Otherwise path B.
 *
 * Idempotent: if rooms already exist the whole setup is skipped, so re-runs
 * in the same environment work without tearing down and recreating everything.
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

async function haIsReachable(): Promise<boolean> {
  try {
    const res = await fetch(`${API}/ha/entities?domain=climate`);
    if (!res.ok) return false;
    const entities: unknown[] = await res.json();
    return Array.isArray(entities) && entities.length > 0;
  } catch {
    return false;
  }
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

// ── Path A: UI-based setup using real HA entities ────────────────────────────

async function setupViaUI(): Promise<void> {
  console.log("[e2e] HA detected — configuring via UI (real entity picker)...");

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
      const search = tc.entityId.split(".")[1].split("_")[0]; // "downstairs"|"upstairs"
      await page.locator(".entity-picker input").fill(search);
      // Dropdown requires a live HA connection; this will time out without Docker
      await page.waitForSelector(".entity-dropdown", { timeout: 20_000 });
      await page.locator(".entity-option").filter({ hasText: tc.entityId }).click();

      await page.locator("#add-thermo-name").fill(tc.name);
      await page.getByRole("button", { name: "Register", exact: true }).click();
      await page.waitForSelector(".modal", { state: "detached" });
    }

    // ── Step 2: Create rooms and configure sensors / vents ───────────────────
    await page.goto(`${BASE_URL}/rooms`);
    await page.waitForSelector(".loading", { state: "detached", timeout: 20_000 });
    await page.waitForLoadState("networkidle");

    for (const def of ROOM_DEFS) {
      console.log(`[e2e] Creating room: ${def.name}...`);
      await page.getByRole("button", { name: /Add room/i }).click();
      await page.waitForSelector(".modal");
      await page.locator("#room-name").fill(def.name);
      await page.locator("#room-thermostat").selectOption(def.thermostat);
      await page.getByRole("button", { name: /Create room/i }).click();
      // After creating a room the app auto-navigates to RoomConfigure for that room
      await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
      await page.waitForLoadState("networkidle");
      console.log(`[e2e] ✓ Room created, configuring entities...`);

      // Add temperature sensor
      const sensorSearch = def.sensor.split("_")[1]; // "living"|"bedroom"|etc.
      await page.locator('input[placeholder*="temperature sensor"]').fill(sensorSearch);
      await page.waitForSelector(".entity-dropdown", { timeout: 20_000 });
      await page.locator(".entity-option").filter({ hasText: def.sensor }).click();

      // Add vent
      const ventSearch = def.vent.split("_")[1]; // "living"|"bedroom"|etc.
      const ventInput = page.locator('input[placeholder*="vent"]');
      await ventInput.fill(ventSearch);
      await page.waitForSelector(".entity-dropdown", { timeout: 20_000 });
      await page.locator(".entity-option").filter({ hasText: def.vent }).click();

      // After clicking, the dropdown closes and input clears. Wait for the input to be empty.
      await ventInput.waitFor({ state: "visible" });
      await page.waitForFunction(
        () => (document.querySelector('input[placeholder*="vent"]') as HTMLInputElement)?.value === "",
        { timeout: 10_000 }
      );

      // Now click Back button
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
        days_of_week: [0, 1, 2, 3, 4],
        start_time: "08:00",
        end_time: "17:00",
        target_temp: 72.0,
      });
    }
  } finally {
    await browser.close();
  }
}

// ── Path B: REST-only seeding (no HA) ───────────────────────────────────────

async function setupViaREST(): Promise<void> {
  console.log("[e2e] No HA detected — seeding via REST API (mock/no-Docker path)...");

  // Register thermostats directly via REST (entity IDs are pre-known)
  const thermoRes: Array<{ id: string }> = [];
  for (const tc of THERMOSTATS) {
    const res = await post("/thermostats", {
      entity_id: tc.entityId,
      name: tc.name,
    });
    thermoRes.push(await res.json());
  }

  // Map entity_id → DB id for room creation
  const thermoList: Array<{ id: string; entity_id: string }> = await (
    await fetch(`${API}/thermostats`)
  ).json();
  const thermoMap = Object.fromEntries(thermoList.map((t) => [t.entity_id, t.id]));

  // Create rooms and wire up sensors + vents
  for (const def of ROOM_DEFS) {
    const roomRes = await post("/rooms", {
      name: def.name,
      thermostat_id: thermoMap[def.thermostat],
    });
    const room: { id: string } = await roomRes.json();

    await post(`/rooms/${room.id}/sensors`, { entity_id: def.sensor });
    await post(`/rooms/${room.id}/vents`, { entity_id: def.vent });

    if (def.addSchedule) {
      await post(`/rooms/${room.id}/schedules`, {
        days_of_week: [0, 1, 2, 3, 4],
        start_time: "08:00",
        end_time: "17:00",
        target_temp: 72.0,
      });
    }
  }
}

// ── Entry point ──────────────────────────────────────────────────────────────

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
  // keeping entity states (and therefore screenshots) stable.
  await post("/system/dev-mode", { dev_mode: true });

  if (await haIsReachable()) {
    await setupViaUI();
  } else {
    await setupViaREST();
  }

  console.log("[e2e] Setup complete.");
}
