import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures";

/**
 * Eco Suspend visibility rule (Issue #500).
 *
 * The Suspend Eco controls (Dashboard page button + zone-card controls,
 * Thermostats page button + card controls) only render when Eco is actually
 * in play somewhere: a thermostat has Eco enabled, a room carries an explicit
 * per-room Eco opt-in, or a suspension is already active. This spec proves
 * all three configuration states with dedicated goldens:
 *
 *   1. Eco off everywhere        → NO suspend controls anywhere
 *   2. On for a room only        → controls surface (page + that zone's card)
 *   3. On for a thermostat       → controls surface (page + that zone's card)
 *
 * Determinism on the shared stack: every test establishes its own eco-flag
 * state at its start (never relying on residue from a prior test), and
 * afterAll restores the fixture default (Eco off everywhere) so no other
 * spec's goldens see these controls. The global-setup already configures the
 * outside-temperature entity (sensor.outdoor_temperature, fixed 80 °F), which
 * the enable-Eco API requires — and 80 °F sits inside both Eco thresholds, so
 * enabling Eco here never actually relaxes a target (engine behaviour is
 * unchanged and the page renders stay deterministic).
 */

const API = `${process.env.PLENUM_URL ?? "http://localhost:8099"}/api`;

const UPSTAIRS = "climate.upstairs_thermostat";

async function setThermostatEco(
  entityId: string,
  enabled: boolean,
): Promise<void> {
  const res = await fetch(`${API}/thermostats/${entityId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ eco_mode_enabled: enabled }),
  });
  if (!res.ok)
    throw new Error(
      `setThermostatEco(${entityId}, ${enabled}) → ${res.status}`,
    );
}

// value: true = explicit room opt-in, null = inherit (the fixture default).
async function setBedroomEco(value: true | null): Promise<void> {
  const rooms: Array<{ id: string; name: string }> = await (
    await fetch(`${API}/rooms`)
  ).json();
  const bedroom = rooms.find((r) => r.name === "Bedroom");
  if (!bedroom) throw new Error("Bedroom room not found in fixture");
  const res = await fetch(`${API}/rooms/${bedroom.id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ eco_mode_enabled: value }),
  });
  if (!res.ok) throw new Error(`setBedroomEco(${value}) → ${res.status}`);
}

async function gotoAndSettle(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await page.waitForSelector(".loading", {
    state: "detached",
    timeout: 15_000,
  });
  await page.waitForLoadState("networkidle");
}

test.describe.serial("Eco Suspend visibility (#500)", () => {
  // Restore the fixture default — Eco off everywhere — for every later spec.
  test.afterAll(async () => {
    await setThermostatEco(UPSTAIRS, false);
    await setBedroomEco(null);
  });

  test("eco off everywhere: no suspend controls anywhere", async ({ page }) => {
    // Establish the state explicitly rather than trusting residue.
    await setThermostatEco(UPSTAIRS, false);
    await setBedroomEco(null);

    await gotoAndSettle(page, "/");
    await expect(page.getByTestId("dashboard-eco-suspend-btn")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Suspend Eco" })).toHaveCount(
      0,
    );
    await expect(page).toHaveScreenshot("eco-visibility-off-dashboard.png", {
      fullPage: true,
    });

    await gotoAndSettle(page, "/thermostats");
    await expect(page.getByTestId("thermostats-eco-suspend-btn")).toHaveCount(
      0,
    );
    await expect(page.getByRole("button", { name: "Suspend Eco" })).toHaveCount(
      0,
    );
  });

  test("eco on for a room only: controls surface for that zone", async ({
    page,
  }) => {
    // Thermostats all OFF; the Bedroom (Upstairs zone) opts in at room level.
    await setThermostatEco(UPSTAIRS, false);
    await setBedroomEco(true);

    await gotoAndSettle(page, "/");
    await expect(page.getByTestId("dashboard-eco-suspend-btn")).toBeVisible();
    // Page-level button + the Upstairs zone card's control — and ONLY that
    // zone's (Downstairs has no eco anywhere, so no card control).
    await expect(page.getByRole("button", { name: "Suspend Eco" })).toHaveCount(
      2,
    );
    await expect(page).toHaveScreenshot("eco-visibility-room-dashboard.png", {
      fullPage: true,
    });

    await gotoAndSettle(page, "/thermostats");
    await expect(page.getByTestId("thermostats-eco-suspend-btn")).toBeVisible();
    await expect(page.getByRole("button", { name: "Suspend Eco" })).toHaveCount(
      2,
    );

    // Leave the room opted out again; the next test sets its own state anyway.
    await setBedroomEco(null);
  });

  test("eco on for a thermostat: controls surface for that zone", async ({
    page,
  }) => {
    await setBedroomEco(null);
    await setThermostatEco(UPSTAIRS, true);

    await gotoAndSettle(page, "/");
    await expect(page.getByTestId("dashboard-eco-suspend-btn")).toBeVisible();
    await expect(page.getByRole("button", { name: "Suspend Eco" })).toHaveCount(
      2,
    );
    await expect(page).toHaveScreenshot(
      "eco-visibility-thermostat-dashboard.png",
      {
        fullPage: true,
      },
    );

    await gotoAndSettle(page, "/thermostats");
    await expect(page.getByTestId("thermostats-eco-suspend-btn")).toBeVisible();
    await expect(page.getByRole("button", { name: "Suspend Eco" })).toHaveCount(
      2,
    );

    await setThermostatEco(UPSTAIRS, false);
  });
});
