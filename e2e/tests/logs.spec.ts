import { test, expect } from "./fixtures";

// Both Logs tabs render REAL seeded data in these goldens (the Metrics-page
// pattern, Issue #442): under CI the page pins its time window to the seeded
// demo week (2025-06-01 → 2025-06-08, see frontend/src/ci.tsx CI_LOGS_RANGE
// and backend/demo_seed.py), the Live Feed starts paused so websocket pushes
// from the live engine cannot append mid-run, and live cycles/events (dated
// "now") fall outside the pinned window — they cannot perturb a pixel.
//
// Each golden also expands one row so the detail rendering is covered:
// an event's details JSON in the feed, and a full cycle detail (rooms with
// requested → 🌿 eco-relaxed targets, outside temp, setpoint history) in the
// cycle history.

test("logs live feed", async ({ page }) => {
  await page.goto("/logs");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Expand one seeded event so the golden covers the details-JSON rendering.
  // "Bedroom" is the first (alphabetical) room of the second fixture
  // thermostat — the message is unique in the seeded feed.
  await page.getByText(/Eco Mode relaxed Bedroom/).click();
  await expect(page.locator(".event-details")).toBeVisible();

  // Viewport-only (no fullPage) — the feed container scrolls internally. The
  // datetime-local window inputs jitter sub-pixel at the mobile project's 3×
  // device scale, hence the per-spec maxDiffPixels (same rationale as
  // metrics.spec.ts).
  await expect(page).toHaveScreenshot("logs.png", { maxDiffPixels: 800 });
});

test("logs cycle history with expanded cycle", async ({ page }) => {
  await page.goto("/logs");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.getByRole("button", { name: "Cycle History" }).click();
  await page.waitForSelector("tbody tr", { timeout: 15_000 });

  // Expand the newest seeded cycle — an Eco-relaxed cooling cycle (the demo
  // week's last day is the hottest) — and wait for its detail fetch: the
  // summary cards (setpoint, outside temp), the rooms table with the
  // requested → 🌿 relaxed target, and the setpoint history.
  await page.locator("tbody tr").first().click();
  await expect(page.getByText("Outside temp")).toBeVisible();
  await expect(page.getByText(/Setpoint history/)).toBeVisible();
  await page.waitForLoadState("networkidle");

  await expect(page).toHaveScreenshot("logs-history.png", { maxDiffPixels: 800 });
});

test("logs cycle temp-sample chart modal", async ({ page }) => {
  await page.goto("/logs");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.getByRole("button", { name: "Cycle History" }).click();
  await page.waitForSelector("tbody tr", { timeout: 15_000 });

  // Expand the newest seeded cycle, then open the per-room temperature chart
  // ("View chart") — a modal that had no golden before #458. The seeded
  // demo cycles carry deterministic temp samples (backend/demo_seed.py), so
  // the SVG polylines render identically on every pass.
  await page.locator("tbody tr").first().click();
  await expect(page.getByText("Outside temp")).toBeVisible();
  await page.getByRole("button", { name: "View chart" }).first().click();
  await expect(page.locator(".modal svg polyline").first()).toBeVisible({ timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  await expect(page).toHaveScreenshot("logs-cycle-chart.png", { maxDiffPixels: 800 });
});

test("logs clear-confirm modal", async ({ page }) => {
  await page.goto("/logs");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Open the destructive-action confirmation modal (golden added with #458).
  // Cancel afterwards — confirming would wipe the seeded feed for other specs.
  await page.getByRole("button", { name: "Clear logs" }).click();
  await expect(page.getByText("Clear all event logs?")).toBeVisible();

  await expect(page).toHaveScreenshot("logs-clear-confirm.png", { maxDiffPixels: 800 });

  await page.getByRole("button", { name: "Cancel" }).click();
});

test("logs retention settings tab", async ({ page }) => {
  await page.goto("/logs");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });

  // The Retention tab is a static settings form; it had no golden before #458.
  await page.getByRole("button", { name: "Retention" }).click();
  // exact: true — a bare substring match also hits the "Event log retention
  // (days)" form label and trips Playwright's strict mode.
  await expect(page.getByText("Log Retention", { exact: true })).toBeVisible();
  await page.waitForLoadState("networkidle");

  await expect(page).toHaveScreenshot("logs-retention.png");
});
