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

async function put(path: string, body: unknown): Promise<Response> {
  const res = await fetch(`${API}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "(no body)");
    throw new Error(`PUT ${API}${path} → ${res.status}: ${text}`);
  }
  return res;
}

// Occupancy sensor defined in e2e/fixtures/ha-config/configuration.yaml. Wired
// to the Office room so it resolves to `via Presence` in the goldens (#456).
const PRESENCE_ENTITY = "binary_sensor.office_occupancy";
// Mon–Fri. The pinned clock (PLENUM_CLOCK_OVERRIDE) lands on a Wednesday.
const WEEKDAYS = [0, 1, 2, 3, 4];

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

/**
 * Pick a schedule target temperature in the active display unit (#231).
 *
 * The schedules POST endpoint converts the value via `_to_f(value, unit)` —
 * so a hard-coded 72.0 means "72°F" under `TEMPERATURE_UNIT=F` and "72°C"
 * under `TEMPERATURE_UNIT=C`. 72°C trips the 4.4–32.2°C valid-range check
 * (400). Return 72 under °F and 22 under °C — both ~71.6°F stored, both
 * comfortable, both well inside the valid range.
 */
async function scheduleTargetTemp(): Promise<number> {
  try {
    const res = await fetch(`${API}/settings`);
    if (!res.ok) return 72.0;
    const data: { temperature_unit?: string } = await res.json();
    return data.temperature_unit === "C" ? 22.0 : 72.0;
  } catch {
    return 72.0;
  }
}

const THERMOSTATS = [
  { entityId: "climate.downstairs_thermostat", name: "Downstairs Thermostat" },
  { entityId: "climate.upstairs_thermostat", name: "Upstairs Thermostat" },
] as const;

// Rooms are seeded so that, at the pinned clock instant (Wed 10:00 ET), each
// active-status permutation is represented exactly once in the goldens (#456);
// the per-room schedule/presence seeding lives in seedRoomStatusFixtures().
//   Living Room → schedule active now (+ a later "then" block)
//   Bedroom     → idle now, upcoming schedule ("next … Wed 6:00 PM")
//   Office      → presence active (occupancy sensor + presence temp)
//   Kitchen     → plain idle baseline
const ROOM_DEFS = [
  {
    name: "Living Room",
    thermostat: "climate.downstairs_thermostat",
    sensor: "sensor.living_room_temperature",
    vent: "cover.living_room_vent",
  },
  {
    name: "Bedroom",
    thermostat: "climate.upstairs_thermostat",
    sensor: "sensor.bedroom_temperature",
    vent: "cover.bedroom_vent",
  },
  {
    name: "Kitchen",
    thermostat: "climate.downstairs_thermostat",
    sensor: "sensor.kitchen_temperature",
    vent: "cover.kitchen_vent",
  },
  {
    name: "Office",
    thermostat: "climate.upstairs_thermostat",
    sensor: "sensor.office_temperature",
    vent: "cover.office_vent",
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
      // Scope to the modal — the Thermostats page itself now mounts an
      // EntityPicker via OutsideTempPicker, so `.entity-picker input`
      // alone is ambiguous (strict-mode violation in Playwright).
      const search = tc.entityId.split(".")[1].split("_")[0]; // "downstairs"|"upstairs"
      await page.locator(".modal .entity-picker input").fill(search);
      // Dropdown requires a live HA connection; this will time out without Docker
      await page.waitForSelector(".modal .entity-dropdown", { timeout: 20_000 });
      await page.locator(".modal .entity-option").filter({ hasText: tc.entityId }).click();

      await page.locator("#add-thermo-name").fill(tc.name);
      // Airflow-floor (#213): total vent count is required at registration.
      // Use a constant — the value only matters for engine behaviour, not
      // for the round-trip / golden screenshots these e2e tests exist for.
      await page.locator("#add-thermo-total-vents").fill("8");
      await page.getByRole("button", { name: "Register", exact: true }).click();
      await page.waitForSelector(".modal", { state: "detached" });
    }

    // ── Step 2: Create rooms and configure sensors / vents ───────────────────
    for (const def of ROOM_DEFS) {
      console.log(`[e2e] Creating room: ${def.name}...`);
      // Start each iteration from a freshly-loaded, settled room list. Creating
      // the previous room re-renders the list, which can detach/re-mount the
      // "Add room" button mid-click ("element is not stable / detached from the
      // DOM"). A fresh navigation + load-state wait guarantees a stable button
      // before we click it. (#329)
      await page.goto(`${BASE_URL}/rooms`);
      await page.waitForSelector(".loading", { state: "detached", timeout: 20_000 });
      await page.waitForLoadState("networkidle");
      const addRoomBtn = page.getByRole("button", { name: /Add room/i });
      await addRoomBtn.waitFor({ state: "visible" });
      await addRoomBtn.click();
      // "+ Add room" now swaps to a full-page settings view (not a modal), so
      // wait for the room-name field rather than a `.modal` container.
      await page.waitForSelector("#room-name");
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

      // Wait for any loading state to clear before clicking Back (same pattern as after room creation)
      await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });

      // Now click Back button (labeled "← All rooms")
      await page.getByRole("button", { name: /All rooms/i }).click();
      await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
      console.log(`[e2e] ✓ Room "${def.name}" configured and back to room list`);
    }
    console.log("[e2e] All rooms configured successfully!");

    // Schedules, presence wiring, and the presence target-temp are seeded
    // centrally in seedRoomStatusFixtures() (REST — no HA UI interaction), so
    // both the UI and REST setup paths produce the identical status fixtures.
  } finally {
    await browser.close();
  }
}

// ── Path B: REST-only seeding (no HA) ───────────────────────────────────────

async function setupViaREST(): Promise<void> {
  console.log("[e2e] No HA detected — seeding via REST API (mock/no-Docker path)...");

  // Register thermostats directly via REST (entity IDs are pre-known).
  // The API keys thermostats by thermostat_entity_id (no separate DB id) and
  // requires total_vents_count since the airflow floor (#213/#421) — mirror
  // the values the UI path fills in.
  for (const tc of THERMOSTATS) {
    await post("/thermostats", {
      thermostat_entity_id: tc.entityId,
      name: tc.name,
      total_vents_count: 8,
    });
  }

  // Create rooms and wire up sensors + vents
  for (const def of ROOM_DEFS) {
    const roomRes = await post("/rooms", {
      name: def.name,
      thermostat_entity_id: def.thermostat,
    });
    const room: { id: string } = await roomRes.json();

    await post(`/rooms/${room.id}/sensors`, { entity_id: def.sensor });
    await post(`/rooms/${room.id}/vents`, { entity_id: def.vent });
  }
}

// ── Room active-status fixtures (schedules / presence) ───────────────────────
//
// Seeds the four status permutations the Rooms/Room-detail goldens capture,
// evaluated against the pinned clock (PLENUM_CLOCK_OVERRIDE = Wed 2025-06-04
// 10:00 ET). Runs once, via REST, after rooms exist — identical on both the UI
// (path A) and REST (path B) setup paths. See ROOM_DEFS for the per-room intent.

async function postSchedule(
  roomId: string,
  startTime: string,
  endTime: string,
  targetTemp: number
): Promise<void> {
  await post(`/rooms/${roomId}/schedules`, {
    days_of_week: WEEKDAYS,
    start_time: startTime,
    end_time: endTime,
    target_temp: targetTemp,
  });
}

// Poll the active-status endpoint until the Office room reports `presence` as
// its source. The engine arms the holdover from the continuously-"on" occupancy
// sensor on its next tick (60s cadence), so this waits up to two ticks. Failing
// loudly here is deliberate: a golden captured before presence armed would bake
// in the wrong state. Only meaningful on the HA (golden-generating) path.
async function waitForPresenceActive(roomId: string): Promise<void> {
  const deadline = Date.now() + 130_000;
  while (Date.now() < deadline) {
    try {
      const res = await post("/rooms/active-status", { room_ids: [roomId] });
      const data: Record<string, { source?: string }> = await res.json();
      if (data[roomId]?.source === "presence") {
        console.log("[e2e] Office presence holdover armed — source=presence");
        return;
      }
    } catch {
      // endpoint not ready yet — keep polling
    }
    await new Promise((r) => setTimeout(r, 3000));
  }
  throw new Error("Office room never reached source=presence within 130s");
}

async function seedRoomStatusFixtures(haReachable: boolean): Promise<void> {
  const rooms: Array<{ id: string; name: string }> = await (await fetch(`${API}/rooms`)).json();
  const idByName = new Map(rooms.map((r) => [r.name, r.id]));
  const temp = await scheduleTargetTemp();

  const living = idByName.get("Living Room");
  if (living) {
    // Active at Wed 10:00 → "🎯 … via Schedule"; the later block is excluded
    // from the "current" match and surfaces as the "then … Wed 8:00 PM" line,
    // so this one room renders both the active and the upcoming-schedule
    // states. Living Room is the only room schedule-flow.spec never touches,
    // so its seeded blocks survive that spec's beforeAll/afterAll resets and
    // stay identical across the update→verify golden passes.
    //
    // 20:00 start (not 18:00): temperature-units.spec's schedule round-trip
    // adds an 18:00–20:00 block here, and the overlap check is a strict
    // [start,end) — an 18:00–20:00 block sits flush against 20:00–22:00
    // without overlapping. Keep that gap clear if you retime this block.
    await postSchedule(living, "08:00", "17:00", temp);
    await postSchedule(living, "20:00", "22:00", temp);
  }

  // Bedroom / Kitchen are intentionally left with no schedule (the idle
  // baseline). They can't hold a golden-visible schedule: schedule-flow.spec
  // reserves Bedroom/Kitchen/Office and clears them in beforeAll/afterAll, so a
  // seed here would render on the first screenshot pass but be gone by the
  // verify pass — the exact nondeterminism this change exists to remove. The
  // upcoming-schedule state is covered by Living Room's "then" line above.

  const office = idByName.get("Office");
  if (office) {
    // Presence needs a target temp (room-level takes priority over the
    // thermostat default) or _resolve_room falls through to idle.
    await put(`/rooms/${office}`, { system_wide_temp: temp });
    await post(`/rooms/${office}/presence`, { entity_id: PRESENCE_ENTITY });
    if (haReachable) {
      await waitForPresenceActive(office);
    } else {
      // No HA cache to read the sensor from — the continuous-presence refresh
      // can't arm the holdover. This is the non-golden REST path, so log and
      // move on rather than blocking on a state that can't occur here.
      console.log("[e2e] No HA — skipping Office presence-active wait (non-golden path)");
    }
  }

  // Kitchen is intentionally left with no schedule/presence — the idle baseline.
  console.log("[e2e] Room active-status fixtures seeded");
}

// ── Demo metrics + logs seeding (Issue #442) ─────────────────────────────────
//
// The Metrics-page charts AND the Logs page (Live Feed + Cycle History) render
// real pixels in the golden screenshots, fed by a deterministic demo dataset
// in a fixed past week (2025-06-01 → 2025-06-07 — the same window the frontend
// pins those pages to under CI, see frontend/src/ci.tsx CI_METRICS_RANGE /
// CI_LOGS_RANGE). The seed endpoint is a pure function of its inputs, and
// reseeding replaces the demo rows wholesale, so both screenshot passes
// (update → verify) see identical data even though the live engine keeps
// logging its own (current-dated, out-of-window) cycles and events.

async function seedDemoMetrics(): Promise<void> {
  const res = await post("/dev/seed-demo-metrics", {});
  const body: {
    seeded_cycles: number;
    seeded_events: number;
    start_date: string;
    end_date: string;
  } = await res.json();
  console.log(
    `[e2e] Seeded ${body.seeded_cycles} demo cycles + ${body.seeded_events} feed events ` +
      `over ${body.start_date} → ${body.end_date}`
  );
}

// The outside-temperature entity unlocks the scatter/outside-temp charts'
// summary tile and silences the "not configured" banner. The PUT validates the
// entity against HA, so it can only succeed on the Docker (path A) stack —
// tolerate failure on the no-HA path.
async function configureOutsideTempEntity(): Promise<void> {
  try {
    const res = await fetch(`${API}/settings/outside-temp-entity`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entity_id: "sensor.outdoor_temperature" }),
    });
    console.log(
      res.ok
        ? "[e2e] Outside-temperature entity configured"
        : `[e2e] Outside-temperature entity skipped (${res.status})`
    );
  } catch {
    console.log("[e2e] Outside-temperature entity skipped (request failed)");
  }
}

// ── Entry point ──────────────────────────────────────────────────────────────

export default async function globalSetup(): Promise<void> {
  await waitForAddon();

  // Auth leg (#373): the stack runs require_auth=true with no Supervisor, so the
  // unauthenticated seeding below would 401 — and the auth specs (login page,
  // MCP-token card, settings) don't need seeded rooms/metrics. Skip it all.
  if (process.env.PLENUM_E2E_AUTH === "1") {
    console.log("[e2e] Auth leg — skipping entity/metrics seeding (require_auth on)");
    return;
  }

  // Enable dev mode — prevents the engine from issuing real HA service calls,
  // keeping entity states (and therefore screenshots) stable. Idempotent, and
  // also the gate for the demo-metrics seed below.
  await post("/system/dev-mode", { dev_mode: true });

  // Idempotent: skip room/thermostat creation if already seeded. The status
  // fixtures (schedules/presence) are seeded together with the rooms, inside
  // this fresh-setup branch, so a re-run against the same stack (e.g. the
  // update→verify screenshot double-pass) doesn't create duplicate schedules.
  const roomsRes = await fetch(`${API}/rooms`);
  const rooms: Array<{ id: string }> = await roomsRes.json();
  if (rooms.length > 0) {
    console.log("[e2e] Rooms already configured — skipping entity setup");
  } else {
    const haReachable = await haIsReachable();
    if (haReachable) {
      await setupViaUI();
    } else {
      await setupViaREST();
    }
    await seedRoomStatusFixtures(haReachable);
  }

  // Both idempotent — safe on every run against the same stack.
  await configureOutsideTempEntity();
  await seedDemoMetrics();

  console.log("[e2e] Setup complete.");
}
