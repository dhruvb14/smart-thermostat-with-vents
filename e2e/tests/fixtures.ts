import { test as base, expect } from "@playwright/test";

// The browser's wall clock, pinned to the SAME absolute instant the backend is
// pinned to via PLENUM_CLOCK_OVERRIDE in docker-compose.test.yml
// (Wed 2025-06-04 10:00 ET = 14:00Z). Keeping both clocks in sync means any
// client-side relative-time rendering agrees with the backend-computed status
// line, so the two never drift apart between the update and verify screenshot
// passes (#456). Backend-derived values (the schedule status) are the load-
// bearing fix; this browser pin is defense-in-depth for anything the frontend
// derives from `Date.now()` that isn't already wrapped in `<Frozen>`.
export const CI_CLOCK_INSTANT = "2025-06-04T14:00:00Z";

// Auto-fixture: runs before every test in any spec that imports from this file.
// Injects a style that makes .nav-version invisible so version bumps in
// config.yaml don't invalidate all golden screenshots.
//
// It also pins `scrollbar-gutter: stable`: a fullPage screenshot renders the
// whole page on one surface, so the vertical scrollbar a viewport-height
// render needs disappears, the content re-wraps ~15px wider, and any text
// sitting on a wrap boundary makes the page height bi-stable — consecutive
// captures alternate between two heights (observed as 6799↔6801px on the
// Thermostats settings panel) and Playwright's "two consecutive stable
// screenshots" check times out ~50% of runs. Reserving the gutter makes the
// viewport and capture layouts identical, on every page.
export const test = base.extend<{ hideNavVersion: void }>({
  hideNavVersion: [
    async ({ page }, use) => {
      // Fix Date.now()/new Date() to the pinned instant while leaving timers
      // running (so the app's polling/React work is unaffected). Set before the
      // spec navigates so the value is in effect from first paint.
      await page.clock.setFixedTime(new Date(CI_CLOCK_INSTANT));
      await page.addInitScript(() => {
        const style = document.createElement("style");
        style.textContent = [
          ".nav-version { visibility: hidden !important; }",
          "html { scrollbar-gutter: stable !important; }",
        ].join("\n");
        document.head.appendChild(style);
      });
      await use();
    },
    { auto: true },
  ],
});

export { expect };
