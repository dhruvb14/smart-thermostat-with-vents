import { test, expect } from "./fixtures";

test("schedules list", async ({ page }) => {
  await page.goto("/schedules");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  await expect(page).toHaveScreenshot("schedules.png", { fullPage: true });
});

test("schedule delete confirmation dialog", async ({ page }) => {
  await page.goto("/schedules");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Expand a room to reveal its schedule rows and the "Del" button.
  await page.getByText("Living Room").first().click();
  await page.getByRole("button", { name: "Del" }).first().click();
  const dialog = page.getByTestId("confirm-dialog");
  await expect(dialog).toBeVisible();
  // Scope to the message <p>, not the whole dialog — matches the pattern
  // used for the other confirm-dialog specs (see rooms.spec.ts).
  await expect(dialog.locator("p")).toContainText("Delete this schedule");

  await expect(page).toHaveScreenshot("schedule-delete-confirm.png", { maxDiffPixels: 800 });

  // Cancel — don't actually delete a schedule other specs depend on.
  await dialog.getByRole("button", { name: "Cancel" }).click();
});
