import { test, expect } from "./fixtures";

test("logs", async ({ page }) => {
  await page.goto("/logs");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Viewport-only (no fullPage) — the event feed grows with every HA state-change
  // event, making the total page height non-deterministic between runs. The
  // 1280×720 viewport captures the stable time-selector + cycle-log area; the
  // event feed is masked in case it scrolls into view.
  await expect(page).toHaveScreenshot("logs.png", {
    mask: [
      page.locator(".event-feed"),
      page.locator("time"),
      page.locator(".timestamp, [class*='timestamp']"),
    ],
  });
});
