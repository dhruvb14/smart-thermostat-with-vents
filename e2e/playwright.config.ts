import { defineConfig, devices } from "@playwright/test";

// Goldens are unit-specific: the visual suite runs once per display unit (the
// e2e.yml matrix sets PLENUM_TEMP_UNIT to F or C) so conversion regressions are
// caught in both directions. Encode the unit in the snapshot filename — e.g.
// dashboard-Fahrenheit-chromium.png vs dashboard-Celsius-chromium.png — so the
// two sets never collide and are easy to tell apart in the file diff. Defaults
// to Fahrenheit for local runs that don't set PLENUM_TEMP_UNIT.
const UNIT_LABEL = process.env.PLENUM_TEMP_UNIT === "C" ? "Celsius" : "Fahrenheit";

export default defineConfig({
  testDir: "./tests",
  outputDir: "./test-results",
  snapshotDir: "./screenshots",
  // Flat path: screenshots/dashboard-Fahrenheit-chromium.png
  snapshotPathTemplate: `{snapshotDir}/{arg}-${UNIT_LABEL}-{projectName}{ext}`,

  retries: 0,
  workers: 1, // sequential — deterministic screenshot order
  // Room for the 30s screenshot-stability budget (see expect.timeout below)
  // plus navigation on the slowest (mobile, 3x-DPI, ~11000px-tall) captures.
  timeout: 60_000,

  use: {
    baseURL: process.env.PLENUM_URL ?? "http://localhost:8099",
    reducedMotion: "reduce",
    viewport: { width: 1280, height: 900 },
    // The vent timeline (and any other localized timestamp) now renders real
    // data in the goldens (Issue #442) — pin the browser's timezone and locale
    // so `toLocaleString()` output is identical on every runner. UTC matches
    // the backend container's TZ, so on-screen times equal the seeded values.
    timezoneId: "UTC",
    locale: "en-US",
  },

  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        // Allow overriding the Chromium binary path via env var.
        // Used locally when the Playwright-bundled binary is unavailable.
        ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
          ? { launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } }
          : {}),
      },
    },
    {
      name: "mobile",
      use: {
        ...devices["iPhone 14"],
        // Override to Chromium so we use the same browser (and sandbox flags) as the
        // chromium project. Without this, Playwright tries to launch WebKit which is
        // not installed in CI (we only install chromium) and fails on root-only runners.
        browserName: "chromium",
        ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
          ? { launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } }
          : {}),
      },
    },
  ],

  globalSetup: "./global-setup.ts",

  expect: {
    // toHaveScreenshot keeps capturing until two CONSECUTIVE shots match, and
    // the very first fullPage capture of a page always differs: the capture's
    // viewport resize settles ~2px of trailing layout, after which every
    // subsequent capture is identical (verified by pixel-diffing consecutive
    // captures — the overlap is byte-identical). Proving stability therefore
    // needs three captures, and a ~6800px-tall page takes ~1.7s per capture
    // (~3s+ on the 3x-DPI mobile project) — that cannot fit the default 5s
    // assertion timeout, which is why only the tallest pages (Thermostats
    // settings panel / modal) "failed to take two consecutive stable
    // screenshots" on random legs. Give the stability loop room instead.
    timeout: 30_000,
    toHaveScreenshot: {
      maxDiffPixels: 100,
      animations: "disabled",
    },
  },
});
