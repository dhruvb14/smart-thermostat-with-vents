import { test, expect } from "./fixtures";

test("dashboard", async ({ page }) => {
  await page.goto("/");

  // Wait for the loading spinner to leave the DOM, then allow the zone-status
  // WebSocket update to arrive before snapping the screenshot.
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // No masks: the volatile bits (the "Updated HH:MM:SS" clock, the live cycle
  // state / active-room count / progress bar) are frozen by the isCI flag under
  // the CI build. See issue #182.
  await expect(page).toHaveScreenshot("dashboard.png", {
    fullPage: true,
  });
});
