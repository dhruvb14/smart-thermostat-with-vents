import { test, expect } from "./fixtures";

/**
 * Visual + interaction coverage of Eco Suspend (Issue #500).
 *
 * Eco Suspend temporarily disables Eco Mode per thermostat until a chosen
 * date/time. Surfaces covered here:
 *   - the global green 🍃 banner (mounted once in App.tsx, every route),
 *   - the Dashboard page-level button + per-zone-card manage control,
 *   - the shared EcoSuspendModal with its thermostat picker,
 *   - the Thermostats page-level button + per-card control.
 *
 * Determinism on the shared stack (same contract as vacation-mode.spec.ts):
 * the suspension is global backend state, so it is created in beforeAll and
 * cleared in afterAll — every other spec's full-page goldens run without the
 * banner, and the chromium + mobile projects plus the update→verify double
 * pass all start from identical state. The resume_at is a fixed, far-future
 * instant so the rendered "resuming …" text is stable — no <Frozen> needed.
 */

const API = `${process.env.PLENUM_URL ?? "http://localhost:8099"}/api`;

const THERMO = "climate.downstairs_thermostat";
// Fixed, far-future resume time so banner/button text is deterministic
// between the update and verify passes.
const RESUME_AT = "2030-06-01T10:00:00.000Z";

async function setEcoSuspend(enabled: boolean): Promise<void> {
  if (enabled) {
    await fetch(`${API}/thermostats/${THERMO}/eco-suspend`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resume_at: RESUME_AT }),
    });
  } else {
    await fetch(`${API}/thermostats/${THERMO}/eco-suspend`, { method: "DELETE" });
  }
}

test.describe.serial("Eco Suspend (#500)", () => {
  test.beforeAll(() => setEcoSuspend(true));
  test.afterAll(() => setEcoSuspend(false));

  test("eco suspend banner and dashboard controls", async ({ page }) => {
    await page.goto("/");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    const banner = page.getByTestId("eco-suspend-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toContainText("Eco Mode suspended");
    await expect(banner).toContainText("Downstairs Thermostat");

    // Element-scoped shot of just the banner.
    await expect(banner).toHaveScreenshot("eco-suspend-banner.png");

    // The page-level button reflects the active suspension, and the suspended
    // zone's card carries its own manage control.
    await expect(page.getByTestId("dashboard-eco-suspend-btn")).toContainText(
      "Eco suspended — manage"
    );
    await expect(page.getByText(/Eco suspended until .* — manage/)).toBeVisible();

    // Full-page shot of the Dashboard so the banner + controls are visible in
    // context. Volatile bits are frozen by the isCI flag (issue #182).
    await expect(page).toHaveScreenshot("eco-suspend-dashboard.png", {
      fullPage: true,
    });
  });

  test("eco suspend modal opens from the banner with a thermostat picker", async ({ page }) => {
    await page.goto("/");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    await page.getByTestId("eco-suspend-banner").click();

    const modal = page.locator(".modal");
    await expect(modal).toBeVisible();
    // Pre-scoped to the suspended thermostat; the picker lets the user choose
    // which thermostat a suspension lands on.
    await expect(modal.locator("#eco-suspend-thermostat")).toHaveValue(THERMO);
    await expect(modal.getByRole("button", { name: "Resume Eco now" })).toBeVisible();

    await expect(modal).toHaveScreenshot("eco-suspend-modal.png");

    // Close without changing anything — this spec must not mutate state
    // beyond its beforeAll/afterAll bracket.
    await modal.getByRole("button", { name: "Close" }).click();
    await expect(modal).not.toBeVisible();
  });

  test("thermostats page surfaces the suspend controls", async ({ page }) => {
    await page.goto("/thermostats");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    // Page-level button (top of the page) reflects the active suspension.
    await expect(page.getByTestId("thermostats-eco-suspend-btn")).toContainText(
      "Eco suspended — manage"
    );

    // The suspended thermostat's card header carries the pre-scoped control.
    const cardBtn = page.getByRole("button", { name: /Eco suspended until/ });
    await expect(cardBtn).toBeVisible();
    await expect(cardBtn).toHaveScreenshot("eco-suspend-card-control.png");

    // Opening it lands on the right thermostat.
    await cardBtn.click();
    const modal = page.locator(".modal");
    await expect(modal.locator("#eco-suspend-thermostat")).toHaveValue(THERMO);
    await modal.getByRole("button", { name: "Close" }).click();
    await expect(modal).not.toBeVisible();
  });
});
