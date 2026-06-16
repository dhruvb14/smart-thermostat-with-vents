import { test, expect } from "./fixtures";

test("room modal — new room form with thermostat select", async ({ page }) => {
  await page.goto("/rooms");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Open the New Room modal
  await page.getByRole("button", { name: /Add room/i }).click();
  await page.waitForSelector(".modal");

  // Screenshot with the thermostat <select> showing its seeded options.
  // No masks: the room cards behind the modal backdrop have their countdown
  // timers frozen by the isCI flag under the CI build, and live temps come from
  // the fixed-value fake sensors. See issue #182.
  await expect(page).toHaveScreenshot("room-modal.png", {
    fullPage: true,
  });
});
