import { test, expect } from "./fixtures";

test("room modal — new room form with thermostat select", async ({ page }) => {
  await page.goto("/rooms");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Open the New Room modal
  await page.getByRole("button", { name: /Add room/i }).click();
  const modal = page.locator(".modal");
  await modal.waitFor({ state: "visible" });

  // Screenshot the modal element itself (not fullPage) so the golden is the
  // dialog — including the thermostat <select> and its seeded options — rather
  // than a tall stitched capture of the room list behind the backdrop. The
  // cards behind have their countdown timers frozen by the isCI flag anyway,
  // but capturing just the modal keeps the golden focused and stable (#182).
  await expect(modal).toHaveScreenshot("room-modal.png");
});
