import { test, expect } from "./fixtures";

// Dev mode is enabled by global-setup.ts, so /dev is reachable and the nav
// link is visible.
test("devmode", async ({ page }) => {
  await page.goto("/dev");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Mask any timestamp cells in the live action feed
  await expect(page).toHaveScreenshot("devmode.png", {
    fullPage: true,
    mask: [page.locator("time"), page.locator(".timestamp, [class*='timestamp']")],
  });
});
