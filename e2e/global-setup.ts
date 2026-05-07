/**
 * Playwright global setup — runs once before all tests.
 *
 * 1. Waits for the addon to be healthy.
 * 2. Seeds the addon with rooms, vents, sensors, thermostat configs, and a
 *    schedule so every page has realistic data to render.
 * 3. Enables dev mode so the engine logs rather than controlling HA entities,
 *    keeping entity states stable throughout the test run.
 *
 * Idempotent: if rooms already exist the setup is skipped, so re-runs work
 * without tearing down and recreating the environment.
 */

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

export default async function globalSetup(): Promise<void> {
  await waitForAddon();

  // Idempotent: skip if rooms already seeded
  const roomsRes = await fetch(`${API}/rooms`);
  const rooms: Array<{ id: string }> = await roomsRes.json();
  if (rooms.length > 0) {
    console.log("[e2e] Already seeded — skipping setup");
    return;
  }

  console.log("[e2e] Seeding addon...");

  // Enable dev mode — prevents the engine from issuing real HA service calls,
  // which keeps entity states (and therefore screenshots) stable.
  await post("/system/dev-mode", { dev_mode: true });

  // Register thermostat configs
  await post("/thermostats", {
    thermostat_entity_id: "climate.downstairs_thermostat",
    name: "Downstairs Thermostat",
    min_setpoint: 65.0,
    max_setpoint: 78.0,
    default_temp: 70.0,
    deadband: 2.0,
  });
  await post("/thermostats", {
    thermostat_entity_id: "climate.upstairs_thermostat",
    name: "Upstairs Thermostat",
    min_setpoint: 65.0,
    max_setpoint: 78.0,
    default_temp: 68.0,
    deadband: 2.0,
  });

  // Room definitions — entity IDs match e2e/fixtures/ha-config/configuration.yaml
  const roomDefs = [
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
  ];

  for (const def of roomDefs) {
    const roomRes = await post("/rooms", {
      name: def.name,
      thermostat_entity_id: def.thermostat,
    });
    const room: { id: string } = await roomRes.json();

    await post(`/rooms/${room.id}/sensors`, { entity_id: def.sensor });
    await post(`/rooms/${room.id}/vents`, {
      entity_id: def.vent,
      control_method: "set_position",
    });

    if (def.addSchedule) {
      await post(`/rooms/${room.id}/schedules`, {
        days_of_week: [0, 1, 2, 3, 4], // Mon–Fri
        start_time: "08:00",
        end_time: "17:00",
        target_temp: 72.0,
      });
    }
  }

  console.log("[e2e] Seed complete.");
}
