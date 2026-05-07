import { test, expect } from "@playwright/test";

test("schedules list", async ({ page }) => {
  await page.goto("/schedules");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  await expect(page).toHaveScreenshot("schedules.png", { fullPage: true });
});
