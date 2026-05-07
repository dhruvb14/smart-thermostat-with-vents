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
      use: { ...devices["Desktop Chrome"] },
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
