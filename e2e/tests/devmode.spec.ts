import { test, expect } from "./fixtures";

// Dev mode is enabled by global-setup.ts, so /dev is reachable and the nav
// link is visible.
test("devmode", async ({ page }) => {
  await page.goto("/dev");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Mask timestamps and the entire action-log feed rows — message text and
  // entity names change each run depending on what the engine has processed.
  await expect(page).toHaveScreenshot("devmode.png", {
    fullPage: true,
    mask: [
      page.locator("time"),
      page.locator(".timestamp, [class*='timestamp']"),
      page.locator(".dev-feed-row"),
    ],
  });
});
