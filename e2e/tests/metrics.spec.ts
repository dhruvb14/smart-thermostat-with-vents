import { test, expect } from "./fixtures";

test("metrics", async ({ page }) => {
  await page.goto("/metrics");

  // Metrics page lazy-loads recharts — wait for the Suspense fallback to clear
  await page.waitForSelector(".loading", { state: "detached", timeout: 20_000 });

  // Wait for the page header to appear (rendered once data calls complete).
  // Avoid networkidle: the lazy-loaded recharts chunk keeps connections open
  // longer than the default test timeout on slow machines.
  await page.waitForSelector(".page-header, .page-title", { timeout: 15_000 });

  // Under CI the chart grid is frozen to a placeholder (isCI), so there are no
  // recharts SVG paths to settle; the only remaining content is the static
  // header, the selector form, and the placeholder. The native <input
  // type="date"> controls render with sub-pixel jitter that is amplified ~9× on
  // the mobile project's 3× device-scale viewport (≈288px), tripping the global
  // maxDiffPixels:100. Allow a small per-screenshot tolerance — safe because the
  // page's volatile/data-driven content is already frozen. See issue #182.
  await page.waitForTimeout(1500);

  await expect(page).toHaveScreenshot("metrics.png", {
    fullPage: true,
    maxDiffPixels: 800,
  });
});
