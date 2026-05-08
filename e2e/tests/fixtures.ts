import { test as base, expect } from "@playwright/test";

// Auto-fixture: runs before every test in any spec that imports from this file.
// Injects a style that makes .nav-version invisible so version bumps in
// config.yaml don't invalidate all golden screenshots.
export const test = base.extend<{ hideNavVersion: void }>({
  hideNavVersion: [
    async ({ page }, use) => {
      await page.addInitScript(() => {
        const style = document.createElement("style");
        style.textContent = ".nav-version { visibility: hidden !important; }";
        document.head.appendChild(style);
      });
      await use();
    },
    { auto: true },
  ],
});

export { expect };
