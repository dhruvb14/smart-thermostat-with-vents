import { test, expect } from "./fixtures";

test("logs", async ({ page }) => {
  await page.goto("/logs");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Viewport-only (no fullPage) — the page height would otherwise grow with the
  // cycle log / event feed. No masks: under the CI build the isCI flag freezes
  // both the cycle-log table body and the event feed to fixed placeholders, so
  // the captured area is deterministic. See issue #182.
  await expect(page).toHaveScreenshot("logs.png");
});
