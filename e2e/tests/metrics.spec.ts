import { test, expect } from "./fixtures";

test("metrics", async ({ page }) => {
  await page.goto("/metrics");

  // Metrics page lazy-loads recharts — wait for the Suspense fallback to clear
  await page.waitForSelector(".loading", { state: "detached", timeout: 20_000 });

  // Wait for the page header to appear (rendered once data calls complete).
  // Avoid networkidle: the lazy-loaded recharts chunk keeps connections open
  // longer than the default test timeout on slow machines.
  await page.waitForSelector(".page-header, .page-title", { timeout: 15_000 });

  // Extra pause for SVG paths to settle after recharts renders
  await page.waitForTimeout(1500);

  await expect(page).toHaveScreenshot("metrics.png", { fullPage: true });
});
