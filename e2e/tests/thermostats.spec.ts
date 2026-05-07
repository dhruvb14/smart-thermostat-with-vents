import { test, expect } from "./fixtures";

test("thermostats", async ({ page }) => {
  await page.goto("/thermostats");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  await expect(page).toHaveScreenshot("thermostats.png", { fullPage: true });
});
