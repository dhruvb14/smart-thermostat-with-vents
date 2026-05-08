import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  outputDir: "./test-results",
  snapshotDir: "./screenshots",
  // Flat path: screenshots/dashboard-chromium.png
  snapshotPathTemplate: "{snapshotDir}/{arg}-{projectName}{ext}",

  retries: 0,
  workers: 1, // sequential — deterministic screenshot order

  use: {
    baseURL: process.env.PLENUM_URL ?? "http://localhost:8099",
    reducedMotion: "reduce",
    viewport: { width: 1280, height: 900 },
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
    toHaveScreenshot: {
      maxDiffPixels: 100,
      animations: "disabled",
    },
  },
});
