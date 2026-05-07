import { test, expect } from "@playwright/test";

test("dashboard", async ({ page }) => {
  await page.goto("/");

  // Wait for the loading spinner to leave the DOM, then allow the zone-status
  // WebSocket update to arrive before snapping the screenshot.
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Mask the subtitle ("Updated HH:MM:SS" changes every load) and stat values
  // (temperatures from live HA sensor states that vary between runs).
  await expect(page).toHaveScreenshot("dashboard.png", {
    fullPage: true,
    mask: [page.locator(".page-subtitle"), page.locator(".stat-value")],
  });
});
