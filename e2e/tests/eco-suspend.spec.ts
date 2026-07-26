import { test, expect, captureModal } from "./fixtures";

/**
 * Visual + interaction coverage of Eco Suspend (Issue #500).
 *
 * Eco Suspend temporarily disables Eco Mode per thermostat until a chosen
 * date/time. Suspensions are fully independent per thermostat — this spec
 * seeds BOTH fixture thermostats with two DIFFERENT resume dates and asserts
 * each surface shows its own date. Surfaces covered:
 *   - the global green 🍃 banner (mounted once in App.tsx, every route),
 *   - the Dashboard page-level button + per-zone-card manage controls,
 *   - the shared EcoSuspendModal with its thermostat picker (per-thermostat
 *     state when switching),
 *   - the Thermostats page-level button + per-card controls.
 *
 * Determinism on the shared stack (same contract as vacation-mode.spec.ts):
 * the suspensions are global backend state, so they are created in beforeAll
 * and cleared in afterAll — every other spec's full-page goldens run without
 * the banner, and the chromium + mobile projects plus the update→verify
 * double pass all start from identical state. The resume_at values are
 * fixed, far-future instants so the rendered text is stable (Playwright pins
 * timezone UTC + locale en-US) — no <Frozen> needed.
 */

const API = `${process.env.PLENUM_URL ?? "http://localhost:8099"}/api`;

const DOWNSTAIRS = "climate.downstairs_thermostat";
const UPSTAIRS = "climate.upstairs_thermostat";
// Two different fixed, far-future resume times — one per thermostat — so the
// per-thermostat independence is visible in the goldens.
const DOWNSTAIRS_RESUME_AT = "2030-06-01T10:00:00.000Z";
const UPSTAIRS_RESUME_AT = "2030-07-04T18:00:00.000Z";
// The exact strings the UI renders under the pinned UTC/en-US browser.
const DOWNSTAIRS_LOCAL = "6/1/2030, 10:00:00 AM";
const UPSTAIRS_LOCAL = "7/4/2030, 6:00:00 PM";

async function suspend(thermostat: string, resumeAt: string): Promise<void> {
  await fetch(`${API}/thermostats/${thermostat}/eco-suspend`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resume_at: resumeAt }),
  });
}

async function clear(thermostat: string): Promise<void> {
  await fetch(`${API}/thermostats/${thermostat}/eco-suspend`, { method: "DELETE" });
}

test.describe.serial("Eco Suspend (#500)", () => {
  test.beforeAll(async () => {
    await suspend(DOWNSTAIRS, DOWNSTAIRS_RESUME_AT);
    await suspend(UPSTAIRS, UPSTAIRS_RESUME_AT);
  });
  test.afterAll(async () => {
    await clear(DOWNSTAIRS);
    await clear(UPSTAIRS);
  });

  test("banner lists both thermostats with their own resume dates", async ({ page }) => {
    await page.goto("/");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    const banner = page.getByTestId("eco-suspend-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toContainText("Eco Mode suspended");
    // ONE banner, both thermostats, each with its OWN date.
    await expect(banner).toContainText("Downstairs Thermostat");
    await expect(banner).toContainText(DOWNSTAIRS_LOCAL);
    await expect(banner).toContainText("Upstairs Thermostat");
    await expect(banner).toContainText(UPSTAIRS_LOCAL);

    // Element-scoped shot of just the banner.
    await expect(banner).toHaveScreenshot("eco-suspend-banner.png");

    // The page-level button reflects the active suspensions, and EACH
    // suspended zone card carries its own manage control with its own date.
    await expect(page.getByTestId("dashboard-eco-suspend-btn")).toContainText(
      "Eco suspended — manage"
    );
    await expect(
      page.getByText(`Eco suspended until ${DOWNSTAIRS_LOCAL} — manage`)
    ).toBeVisible();
    await expect(page.getByText(`Eco suspended until ${UPSTAIRS_LOCAL} — manage`)).toBeVisible();

    // Full-page shot of the Dashboard so the banner + both controls are
    // visible in context. Volatile bits are frozen by the isCI flag (#182).
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

    // Close without changing anything — this spec must not mutate state
    // beyond its beforeAll/afterAll bracket.
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
