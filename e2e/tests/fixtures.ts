import { test as base, expect, type Locator, type Page } from "@playwright/test";
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
 * Screenshot a modal, expanding it first if — and only if — its content
 * overflows the box it is captured in.
 *
 * `.modal` ships `max-height: 90vh; overflow-y: auto` inside a
 * `position: fixed` backdrop. Once its content outgrows that, the element
 * capture is clipped to 90vh AND pinned to whatever scroll offset the last
 * interaction left behind. The schedule modal did exactly this the moment it
 * gained a fourth field: the goldens silently stopped covering the title, the
 * day picker and the error banner — the part of a form a rendering regression
 * is most likely to show up in.
 *
 * That failure is dangerous because it fails OPEN. Nothing errors; the golden
 * just regenerates smaller, the bot posts its usual "review the changed PNGs"
 * comment, and the diff looks like an ordinary content change. Coverage
 * shrinks with no signal. So this is not something to remember to opt into —
 * route every modal capture through here and it cannot happen.
 *
 * The overflow check is what keeps it free: a modal that already fits is
 * captured untouched, byte-identical to before, so adopting this in a spec
 * does not churn its goldens. Only a modal that would have been clipped gets
 * the unpinned backdrop (absolute + top-aligned, which lets the PAGE scroll,
 * since Playwright cannot scroll a fixed overlay to stitch in the rest).
 *
 * Both the style and the scroll it causes are undone afterwards. Skipping the
 * scroll restore once pushed the NEXT capture — a room card — under the sticky
 * nav, eating its title and active-count badge, so three card goldens
 * regressed while the modal ones were being fixed.
 *
 * Screenshot-only: it styles the capture, never the shipped app. Page-level
 * captures with a confirm dialog open (`room-delete-confirm`,
 * `logs-clear-confirm`, `mcp-token-revoke-confirm`, `schedule-delete-confirm`)
 * deliberately do NOT use this — those dialogs are short, never overflow, and
 * unpinning the backdrop would shift them from centred to top-aligned.
 */
export async function captureModal(
  page: Page,
  modal: Locator,
  name: string,
  options?: { maxDiffPixels?: number; timeout?: number },
): Promise<void> {
  // +1 absorbs sub-pixel rounding on fractional-DPI projects, so a modal that
  // fits exactly is not needlessly expanded.
  const overflows = await modal.evaluate((el) => el.scrollHeight > el.clientHeight + 1);
  if (!overflows) {
    await expect(modal).toHaveScreenshot(name, options);
    return;
  }

  const scrollY = await page.evaluate(() => window.scrollY);
  const tag = await page.addStyleTag({
    content: [
      ".modal-backdrop { position: absolute !important; align-items: flex-start !important; }",
      ".modal { max-height: none !important; overflow-y: visible !important; }",
    ].join("\n"),
  });
  try {
    await expect(modal).toHaveScreenshot(name, options);
  } finally {
    await tag.evaluate((el: Element) => el.remove());
    await page.evaluate((y) => window.scrollTo(0, y), scrollY);
  }
}

export { expect };
