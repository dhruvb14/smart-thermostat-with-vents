import { test, expect } from "./fixtures";

// Settings lives inside the Thermostats page (per-thermostat settings panel).
// Navigate to /thermostats since there is no standalone /settings route.
test("settings panel", async ({ page }) => {
  await page.goto("/thermostats");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Open the settings/edit panel for the first thermostat
  const editBtn = page.getByRole("button", { name: /edit|settings|configure/i }).first();
  if (await editBtn.isVisible()) {
    await editBtn.click();
    await page.waitForLoadState("networkidle");
  }

  await expect(page).toHaveScreenshot("settings.png", { fullPage: true });
});
