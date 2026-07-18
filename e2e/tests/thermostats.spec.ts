import { test, expect } from "./fixtures";

test("thermostats", async ({ page }) => {
  await page.goto("/thermostats");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  await expect(page).toHaveScreenshot("thermostats.png", { fullPage: true });
});

test("thermostat remove confirmation dialog", async ({ page }) => {
  await page.goto("/thermostats");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  await page.getByRole("button", { name: "Remove" }).first().click();
  const dialog = page.getByTestId("confirm-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(/Remove thermostat/)).toBeVisible();

  await expect(page).toHaveScreenshot("thermostat-remove-confirm.png", { maxDiffPixels: 800 });

  // Cancel — don't actually remove a thermostat other specs depend on.
  await dialog.getByRole("button", { name: "Cancel" }).click();
});
