import { test, expect } from "./fixtures";

test("thermostat modal — entity picker open", async ({ page, request }) => {
  await page.goto("/thermostats");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Detect whether a real HA instance is reachable.
  // If not (no-Docker / Claude Cloud path), mock the entity endpoint so the
  // EntityPicker dropdown renders realistically even without HA.
  let hasHA = false;
  try {
    const entRes = await request.get("/api/ha/entities?domain=climate");
    if (entRes.ok()) {
      const entities: unknown[] = await entRes.json();
      hasHA = Array.isArray(entities) && entities.length > 0;
    }
  } catch {
    hasHA = false;
  }

  if (!hasHA) {
    // Intercept /api/ha/entities and return the two fake climate entities so
    // the dropdown renders.  This is the cloud/no-Docker path only.
    await page.route("**/api/ha/entities**", (route) =>
      route.fulfill({
        contentType: "application/json",
        body: JSON.stringify([
          {
            entity_id: "climate.downstairs_thermostat",
            friendly_name: "Downstairs Thermostat",
            domain: "climate",
          },
          {
            entity_id: "climate.upstairs_thermostat",
            friendly_name: "Upstairs Thermostat",
            domain: "climate",
          },
        ]),
      })
    );
  }

  // Open the Register Thermostat modal
  await page.getByRole("button", { name: /Register thermostat/i }).click();
  await page.waitForSelector(".modal");

  // Focus the EntityPicker input and type to trigger the autocomplete lookup.
  await page.locator(".entity-picker input").click();
  await page.locator(".entity-picker input").fill("thermostat");

  // Allow up to 2 s for the dropdown to appear.
  // With HA: real entities populate from the proxy.
  // Without HA (mock): the intercepted response resolves immediately.
  await page.waitForSelector(".entity-dropdown", { timeout: 5_000 }).catch(() => {
    // If the dropdown still didn't appear, the screenshot captures that state.
  });

  await expect(page).toHaveScreenshot("thermostat-modal.png", { fullPage: true });
});
