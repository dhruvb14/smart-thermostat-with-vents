import { test, expect } from "@playwright/test";

test("logs", async ({ page }) => {
  await page.goto("/logs");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Mask any timestamp cells — they change between test runs
  await expect(page).toHaveScreenshot("logs.png", {
    fullPage: true,
    mask: [page.locator("time"), page.locator(".timestamp, [class*='timestamp']")],
  });
});
