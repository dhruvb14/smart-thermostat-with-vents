import { test, expect } from "@playwright/test";

test("logs", async ({ page }) => {
  await page.goto("/logs");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Mask the live event feed (streams HA state-change events with different
  // timestamps each run) and any explicit timestamp elements in the log table.
  await expect(page).toHaveScreenshot("logs.png", {
    fullPage: true,
    mask: [
      page.locator(".event-feed"),
      page.locator("time"),
      page.locator(".timestamp, [class*='timestamp']"),
    ],
  });
});
