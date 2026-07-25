import { test as base, expect, type Page } from "@playwright/test";
import { AUTH_MODE, SESSION_COOKIE, mintSession } from "../auth-cookie";

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
    async ({ page, baseURL }, use) => {
      // Fix Date.now()/new Date() to the pinned instant while leaving timers
      // running (so the app's polling/React work is unaffected). Set before the
      // spec navigates so the value is in effect from first paint.
      await page.clock.setFixedTime(new Date(CI_CLOCK_INSTANT));
      // Auth leg only (#373): the stack runs require_auth=true with no
      // Supervisor, so authenticate by injecting a valid signed session cookie —
      // this renders the authenticated UI. The login-page spec clears it first
      // to capture the unauthenticated state. No-op for the normal F/C legs.
      if (AUTH_MODE && baseURL) {
        await page
          .context()
          .addCookies([{ name: SESSION_COOKIE, value: mintSession(), url: baseURL }]);
      }
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

/**
 * Let a modal grow to its full height so an ELEMENT screenshot captures all of
 * it. Call once, before the first `expect(modal).toHaveScreenshot(...)`.
 *
 * `.modal` ships `max-height: 90vh; overflow-y: auto` inside a
 * `position: fixed` backdrop. Once its content outgrows the viewport — which
 * the schedule modal did the moment it gained a fourth field — the element
 * capture is clipped to 90vh AND pinned to whatever scroll offset the last
 * interaction left behind. On the mobile project that silently chopped the
 * title, the error banner, the day picker and the start time off the top of
 * every schedule-modal golden, so those goldens stopped covering the part of
 * the form most likely to regress. Playwright cannot scroll a fixed overlay to
 * stitch in the rest, so the backdrop has to be unpinned (absolute and
 * top-aligned, which lets the PAGE scroll) and the cap dropped.
 *
 * Deliberately opt-in rather than an auto-fixture: several specs screenshot the
 * whole PAGE with a confirm dialog open (`room-delete-confirm`,
 * `logs-clear-confirm`, `mcp-token-revoke-confirm`, `schedule-delete-confirm`),
 * and unpinning the backdrop globally would shift every one of those from
 * vertically centred to top-aligned — churning goldens that are not broken.
 * Those dialogs are short and do not overflow, so they do not need this.
 *
 * Screenshot-only: it styles the capture, never the shipped app.
 */
export async function expandModalForCapture(page: Page): Promise<void> {
  await page.addStyleTag({
    content: [
      ".modal-backdrop { position: absolute !important; align-items: flex-start !important; }",
      ".modal { max-height: none !important; overflow-y: visible !important; }",
    ].join("\n"),
  });
}

export { expect };
