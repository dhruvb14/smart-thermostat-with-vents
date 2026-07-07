import { test, expect } from "./fixtures";

// The Metrics charts render REAL pixels in these goldens (Issue #442): under
// CI the page pins its date range to the seeded demo week (2025-06-01 →
// 2025-06-07, see frontend/src/ci.tsx CI_METRICS_RANGE and
// backend/demo_seed.py), the dataset is a pure function of the fixture
// thermostats/rooms, and recharts mount animations are disabled
// (chartAnimationActive). Live engine cycles are dated "now" and therefore
// fall outside the pinned window — they cannot perturb a single pixel here.

test("metrics", async ({ page }) => {
  await page.goto("/metrics");

  // Metrics page lazy-loads recharts — wait for the Suspense fallback to clear
  await page.waitForSelector(".loading", { state: "detached", timeout: 20_000 });
  await page.waitForSelector(".page-header, .page-title", { timeout: 15_000 });

  // Home view: summary tiles, the Eco Mode impact section, and the two donuts.
  // Wait for actual chart SVG output AND for every loading placeholder to
  // clear — a lingering skeleton is static under `animations: "disabled"` and
  // would get baked into the update-pass golden, then mismatch the verify
  // pass once the data arrives.
  await page.waitForSelector(".chart-card svg.recharts-surface", { timeout: 20_000 });
  await page.waitForFunction(
    () => !document.querySelector(".skeleton-bar, .skeleton-block, .chart-card .loading"),
    { timeout: 20_000 }
  );

  // The native <input type="date"> controls render with sub-pixel jitter that
  // is amplified ~9× on the mobile project's 3× device-scale viewport,
  // tripping the global maxDiffPixels:100 — hence the per-screenshot
  // tolerance. The data region itself is deterministic (see header comment).
  await expect(page).toHaveScreenshot("metrics.png", {
    fullPage: true,
    maxDiffPixels: 800,
  });
});

test("metrics per-thermostat charts", async ({ page }) => {
  await page.goto("/metrics");
  await page.waitForSelector(".loading", { state: "detached", timeout: 20_000 });
  await page.waitForSelector(".page-header, .page-title", { timeout: 15_000 });

  // Switch to a specific thermostat — this is where the full chart set
  // (heating/cooling hours, duty cycle, scatter, heatmap, vent timeline, …)
  // lives, so it gets its own golden.
  await page
    .locator("select.form-control")
    .first()
    .selectOption("climate.downstairs_thermostat");

  // Full grid fetches ~16 feeds; wait for chart surfaces, the vent-timeline
  // table (the last, table-based card), and for every loading placeholder to
  // clear before capturing.
  await page.waitForSelector(".chart-card svg.recharts-surface", { timeout: 20_000 });
  await page.waitForSelector(".chart-card .data-table", { timeout: 20_000 });
  await page.waitForFunction(
    () => !document.querySelector(".skeleton-bar, .skeleton-block, .chart-card .loading"),
    { timeout: 20_000 }
  );

  await expect(page).toHaveScreenshot("metrics-thermostat.png", {
    fullPage: true,
    maxDiffPixels: 800,
  });
});
