import { test, expect } from "@playwright/test";

test("thermostat modal — entity picker open", async ({ page }) => {
  // Return fake climate entities so the EntityPicker dropdown has results to render.
  // Without a real HA instance the /api/ha/entities endpoint returns an empty array,
  // which would leave the dropdown invisible and make the screenshot useless.
  await page.route("**/api/ha/entities**", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify([
        {
          entity_id: "climate.downstairs_thermostat",
          friendly_name: "Downstairs Thermostat",
        },
        {
          entity_id: "climate.upstairs_thermostat",
          friendly_name: "Upstairs Thermostat",
        },
      ]),
    })
  );

  await page.goto("/thermostats");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Open the Register Thermostat modal
  await page.getByRole("button", { name: /Register thermostat/i }).click();
  await page.waitForSelector(".modal");

  // Click into the EntityPicker input and type to open the autocomplete dropdown
  await page.locator(".entity-picker input").click();
  await page.locator(".entity-picker input").fill("thermostat");
  await page.waitForSelector(".entity-dropdown");

  await expect(page).toHaveScreenshot("thermostat-modal.png", { fullPage: true });
});
