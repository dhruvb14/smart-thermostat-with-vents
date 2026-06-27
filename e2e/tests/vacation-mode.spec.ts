import { test, expect } from "./fixtures";

/**
 * Visual coverage of the global "Vacation mode active" banner (Issue #363).
 *
 * The banner is mounted once in App.tsx above <main>, so it appears above the
 * page title on every route. It is styled as a card with a green left border —
 * visually parallel to the amber StaleSensorsBanner but green to signal an
 * informational/active state. This spec enables vacation mode, navigates to the
 * Dashboard, and screenshots the banner element.
 *
 * Determinism on the shared stack: vacation mode is global backend state, so —
 * like schedule-flow.spec.ts resets the rooms it touches — this enables vacation
 * mode in beforeAll and disables it in afterAll. That keeps it off for every
 * other spec's full-page goldens (which would otherwise capture the banner), and
 * keeps the chromium + mobile projects and the update→verify double pass all
 * starting from the same state.
 *
 * The "Returning …" timestamp uses a fixed, far-future return_at so the rendered
 * text is stable across the update and verify passes — no <Frozen> needed.
 */

const API = `${process.env.PLENUM_URL ?? "http://localhost:8099"}/api`;

// Fixed, far-future return time so the banner's "Returning …" text is
// deterministic between the update and verify passes.
const RETURN_AT = "2030-06-01T10:00:00.000Z";

async function setVacationMode(enabled: boolean): Promise<void> {
  if (enabled) {
    await fetch(`${API}/settings/vacation-mode`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ return_at: RETURN_AT }),
    });
  } else {
    await fetch(`${API}/settings/vacation-mode`, { method: "DELETE" });
  }
}

test.describe.serial("Vacation mode banner (#363)", () => {
  test.beforeAll(() => setVacationMode(true));
  test.afterAll(() => setVacationMode(false));

  test("vacation mode banner", async ({ page }) => {
    await page.goto("/");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    const banner = page.getByTestId("vacation-mode-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toContainText("Vacation mode active");

    await expect(banner).toHaveScreenshot("vacation-mode-banner.png");
  });
});
