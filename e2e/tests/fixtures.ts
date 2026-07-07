import { test as base, expect } from "@playwright/test";

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
