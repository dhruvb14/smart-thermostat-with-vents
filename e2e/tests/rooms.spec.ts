import { test, expect } from "./fixtures";

test("rooms list", async ({ page }) => {
  await page.goto("/rooms");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // No masks: the live countdown timers in the status row are frozen by the
  // isCI flag under the CI build; live temps come from the fixed-value fake
  // sensors so they're already stable. See issue #182.
  await expect(page).toHaveScreenshot("rooms.png", {
    fullPage: true,
  });
});

test("room detail — Living Room", async ({ page }) => {
  await page.goto("/rooms");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Open the Living Room detail card / modal
  await page.getByText("Living Room").first().click();
  await page.waitForLoadState("networkidle");

  // Do NOT use fullPage: true here — on mobile the sticky <nav> is duplicated at each
  // viewport-slice boundary during Playwright's fullPage stitching, causing persistent
  // pixel instability. The viewport captures the expanded room detail which is what we
  // care about. No masks: the per-second countdown timers are frozen by the isCI
  // flag under the CI build. See issue #182.
  await expect(page).toHaveScreenshot("room-detail.png");
});

test("room delete confirmation dialog", async ({ page }) => {
  await page.goto("/rooms");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  await page.getByRole("button", { name: "Delete" }).first().click();
  const dialog = page.getByTestId("confirm-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(/Delete room/)).toBeVisible();

  await expect(page).toHaveScreenshot("room-delete-confirm.png", { maxDiffPixels: 800 });

  // Cancel — don't actually delete a room other specs depend on.
  await dialog.getByRole("button", { name: "Cancel" }).click();
});
