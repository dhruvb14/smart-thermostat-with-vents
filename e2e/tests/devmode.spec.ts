import { test, expect } from "./fixtures";

// Dev mode is enabled by global-setup.ts, so /dev is reachable and the nav
// link is visible.
test("devmode", async ({ page }) => {
  await page.goto("/dev");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // No masks: under the CI build the isCI flag freezes the action-log feed to
  // its empty state and the live cycle/hvac badges to "—", so the page is
  // deterministic. See issue #182.
  await expect(page).toHaveScreenshot("devmode.png", {
    fullPage: true,
  });
});
