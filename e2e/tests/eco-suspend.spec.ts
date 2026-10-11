import {
  test,
  expect,
  captureModal,
  settleForFullPageShot,
  markDocument,
  expectSameDocument,
} from "./fixtures";

/**
 * Visual + interaction coverage of Eco Suspend (Issue #500), with the
 * suspensions created the way a user creates them — through the Dashboard UI
 * (#678).
 *
 * Eco Suspend temporarily disables Eco Mode per thermostat until a chosen
 * date/time. Suspensions are fully independent per thermostat — this spec
 * suspends BOTH fixture thermostats, one at a time and through different
 * entry points, with two DIFFERENT resume dates, and asserts each surface
 * shows its own date. Surfaces covered:
 *   - the global green 🍃 banner (mounted once in App.tsx, every route),
 *   - the Dashboard page-level button + per-zone-card controls,
 *   - the shared EcoSuspendModal with its thermostat picker (per-thermostat
 *     state when switching),
 *   - the Thermostats page-level button + per-card controls.
 *
 * The first test asserts the banner renders after each suspension in the SAME
 * document — no reload. That is the #678 regression: the banner used to read
 * its state only on mount, so suspending from the Dashboard left it missing
 * until a full reload (a force-close in the PWA). Seeding the suspensions
 * through the API before page load, as this spec originally did, could never
 * catch that. The later tests build on the state the first one created
 * (`describe.serial` skips them if it fails).
 *
 * Determinism on the shared stack (same contract as vacation-mode.spec.ts):
 * the suspensions and the Eco flags are global backend state, so beforeAll
 * clears any suspension and turns Eco on for both thermostats (the Suspend
 * controls only render while Eco is in play — eco-suspend-visibility.spec.ts),
 * and afterAll clears the suspensions and restores the fixture default, Eco
 * off everywhere. Every other spec's goldens run without the banner, and every
 * project plus the update→verify double pass starts from identical state.
 * Enabling Eco changes no engine behaviour here: global-setup's outdoor sensor
 * reads a fixed 80 °F, inside both Eco thresholds, so no target is relaxed.
 * The resume times are fixed, far-future values typed into the picker, so the
 * rendered text is stable (Playwright pins timezone UTC + locale en-US, so the
 * typed local time IS the stored instant) — no <Frozen> needed.
 */

const API = `${process.env.PLENUM_URL ?? "http://localhost:8099"}/api`;

const DOWNSTAIRS = "climate.downstairs_thermostat";
const UPSTAIRS = "climate.upstairs_thermostat";
// Two different fixed, far-future resume times — one per thermostat — so the
// per-thermostat independence is visible in the goldens. What the user types
// into the datetime-local picker…
const DOWNSTAIRS_RESUME_INPUT = "2030-06-01T10:00";
const UPSTAIRS_RESUME_INPUT = "2030-07-04T18:00";
// …and the exact strings the UI renders under the pinned UTC/en-US browser.
const DOWNSTAIRS_LOCAL = "6/1/2030, 10:00:00 AM";
const UPSTAIRS_LOCAL = "7/4/2030, 6:00:00 PM";

async function clearSuspension(thermostat: string): Promise<void> {
  const res = await fetch(`${API}/thermostats/${thermostat}/eco-suspend`, { method: "DELETE" });
  if (!res.ok) throw new Error(`clearSuspension(${thermostat}) → ${res.status}`);
}

async function setThermostatEco(thermostat: string, enabled: boolean): Promise<void> {
  const res = await fetch(`${API}/thermostats/${thermostat}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ eco_mode_enabled: enabled }),
  });
  if (!res.ok) throw new Error(`setThermostatEco(${thermostat}, ${enabled}) → ${res.status}`);
}

test.describe.serial("Eco Suspend (#500)", () => {
  test.beforeAll(async () => {
    for (const t of [DOWNSTAIRS, UPSTAIRS]) {
      await clearSuspension(t);
      await setThermostatEco(t, true);
    }
  });
  test.afterAll(async () => {
    for (const t of [DOWNSTAIRS, UPSTAIRS]) {
      await clearSuspension(t);
      await setThermostatEco(t, false);
    }
  });

  test("suspending Eco from the Dashboard shows the banner without a reload", async ({ page }) => {
    await page.goto("/");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    const banner = page.getByTestId("eco-suspend-banner");
    const pageButton = page.getByTestId("dashboard-eco-suspend-btn");
    const modal = page.locator(".modal");
    await expect(banner).toHaveCount(0);
    await expect(pageButton).toContainText("Suspend Eco");
    await markDocument(page);

    // Upstairs first, through the page-level button + the modal's picker. The
    // banner appears in the same document, listing just that thermostat.
    await pageButton.click();
    await modal.locator("#eco-suspend-thermostat").selectOption(UPSTAIRS);
    await modal.getByLabel(/Resume Eco at/i).fill(UPSTAIRS_RESUME_INPUT);
    await modal.getByRole("button", { name: "Suspend Eco", exact: true }).click();
    await modal.waitFor({ state: "detached" });

    await expect(banner).toBeVisible();
    await expect(banner).toContainText("Eco Mode suspended");
    await expect(banner).toContainText("Upstairs Thermostat");
    await expect(banner).toContainText(UPSTAIRS_LOCAL);
    await expect(banner).not.toContainText("Downstairs Thermostat");

    // Then Downstairs, independently, through its own zone card's control
    // (which opens the modal pre-scoped to it). The same banner picks it up.
    const downstairsCard = page.locator(".card-grid > .card").filter({ hasText: DOWNSTAIRS });
    await downstairsCard.getByRole("button", { name: /Suspend Eco/ }).click();
    await expect(modal.locator("#eco-suspend-thermostat")).toHaveValue(DOWNSTAIRS);
    await modal.getByLabel(/Resume Eco at/i).fill(DOWNSTAIRS_RESUME_INPUT);
    await modal.getByRole("button", { name: "Suspend Eco", exact: true }).click();
    await modal.waitFor({ state: "detached" });

    // ONE banner, both thermostats, each with its OWN date — and still the
    // same document: neither suspension needed a reload to show up.
    await expect(banner).toContainText("Downstairs Thermostat");
    await expect(banner).toContainText(DOWNSTAIRS_LOCAL);
    await expect(banner).toContainText("Upstairs Thermostat");
    await expect(banner).toContainText(UPSTAIRS_LOCAL);
    await expectSameDocument(page);

    // Element-scoped shot of just the banner.
    await expect(banner).toHaveScreenshot("eco-suspend-banner.png");

    // The page-level button reflects the active suspensions, and EACH
    // suspended zone card carries its own manage control with its own date.
    await expect(pageButton).toContainText("Eco suspended — manage");
    await expect(
      page.getByText(`Eco suspended until ${DOWNSTAIRS_LOCAL} — manage`)
    ).toBeVisible();
    await expect(page.getByText(`Eco suspended until ${UPSTAIRS_LOCAL} — manage`)).toBeVisible();

    // Full-page shot of the Dashboard so the banner + both controls are
    // visible in context. Volatile bits are frozen by the isCI flag (#182).
    await settleForFullPageShot(page);
    await expect(page).toHaveScreenshot("eco-suspend-dashboard.png", {
      fullPage: true,
    });
  });

  test("modal manages each thermostat's suspension independently", async ({ page }) => {
    await page.goto("/");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    await page.getByTestId("eco-suspend-banner").click();

    const modal = page.locator(".modal");
    await expect(modal).toBeVisible();
    const picker = modal.locator("#eco-suspend-thermostat");
    await expect(modal.getByRole("button", { name: "Resume Eco now" })).toBeVisible();

    // Selecting each thermostat shows ITS OWN suspension date.
    await picker.selectOption(UPSTAIRS);
    await expect(modal).toContainText(`suspended until ${UPSTAIRS_LOCAL}`);
    await picker.selectOption(DOWNSTAIRS);
    await expect(modal).toContainText(`suspended until ${DOWNSTAIRS_LOCAL}`);

    await captureModal(page, modal, "eco-suspend-modal.png");

    // Close without changing anything — the suspensions the first test made
    // must reach the next test, and afterAll, unchanged.
    await modal.getByRole("button", { name: "Close" }).click();
    await expect(modal).not.toBeVisible();
  });

  test("thermostats page carries a pre-scoped control per thermostat", async ({ page }) => {
    await page.goto("/thermostats");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    // Page-level button (top of the page) reflects the active suspensions.
    await expect(page.getByTestId("thermostats-eco-suspend-btn")).toContainText(
      "Eco suspended — manage"
    );

    // Each thermostat's card header carries ITS OWN control with ITS date.
    const downstairsBtn = page.getByRole("button", {
      name: `Eco suspended until ${DOWNSTAIRS_LOCAL}`,
    });
    const upstairsBtn = page.getByRole("button", {
      name: `Eco suspended until ${UPSTAIRS_LOCAL}`,
    });
    await expect(downstairsBtn).toBeVisible();
    await expect(upstairsBtn).toBeVisible();
    await expect(downstairsBtn).toHaveScreenshot("eco-suspend-card-control.png");

    // Opening a card's control lands on THAT thermostat.
    await upstairsBtn.click();
    const modal = page.locator(".modal");
    await expect(modal.locator("#eco-suspend-thermostat")).toHaveValue(UPSTAIRS);
    await expect(modal).toContainText(`suspended until ${UPSTAIRS_LOCAL}`);
    await modal.getByRole("button", { name: "Close" }).click();
    await expect(modal).not.toBeVisible();
  });
});
