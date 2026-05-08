import { test, expect } from "./fixtures";

test("room modal — new room form with thermostat select", async ({ page }) => {
  await page.goto("/rooms");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Open the New Room modal
  await page.getByRole("button", { name: /Add room/i }).click();
  await page.waitForSelector(".modal");

  // Screenshot with the thermostat <select> showing its seeded options
  await expect(page).toHaveScreenshot("room-modal.png", { fullPage: true });
});
