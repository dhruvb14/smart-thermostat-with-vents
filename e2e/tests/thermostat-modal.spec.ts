import { test, expect } from "@playwright/test";

test("thermostat modal — entity picker open", async ({ page }) => {
  await page.goto("/thermostats");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Open the Register Thermostat modal
  await page.getByRole("button", { name: /Register thermostat/i }).click();
  await page.waitForSelector(".modal");

  // Focus the EntityPicker input and type to trigger the autocomplete lookup.
  // When running against a live HA instance the dropdown will show matching
  // climate entities; without HA it remains empty.  Either way we capture the
  // modal's rendering state for visual regression.
  await page.locator(".entity-picker input").click();
  await page.locator(".entity-picker input").fill("thermostat");

  // Allow up to 1 s for the dropdown to appear (requires HA connection).
  // If HA is not available the dropdown simply won't render, and that is an
  // accurate reflection of what a user would see.
  await page.waitForTimeout(1000);

  await expect(page).toHaveScreenshot("thermostat-modal.png", { fullPage: true });
});
