import { test, expect } from "./fixtures";

// The "Configure sensors & vents" page (sensors, the per-vent control-method
// table, presence sensors) had no golden before #458 — it is where the vent
// table's mobile card layout renders, so it needs visual coverage.
test("room configure — sensors & vents page", async ({ page }) => {
  await page.goto("/rooms");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Living Room: the one room whose fixture state no other spec mutates
  // (schedule-flow resets the other rooms), so the golden stays stable
  // across the update → verify passes.
  await page
    .locator(".card")
    .filter({ hasText: "Living Room" })
    .first()
    .getByRole("button", { name: /Configure sensors & vents/ })
    .click();

  const configure = page.getByTestId("room-configure");
  await configure.waitFor({ state: "visible" });
  await page.waitForLoadState("networkidle");

  // Same tall-viewport pattern as room-settings.spec.ts: paint the whole page
  // in one pass so the sticky nav isn't spliced into fullPage stitching.
  const vp = page.viewportSize();
  if (vp) await page.setViewportSize({ width: vp.width, height: 4000 });

  await expect(configure).toHaveScreenshot("room-configure.png");
});
