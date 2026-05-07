import { test, expect } from "@playwright/test";

test("dashboard", async ({ page }) => {
  await page.goto("/");

  // Wait for the loading spinner to leave the DOM, then allow the zone-status
  // WebSocket update to arrive before snapping the screenshot.
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  await expect(page).toHaveScreenshot("dashboard.png", { fullPage: true });
});
