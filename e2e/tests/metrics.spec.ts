import { test, expect } from "@playwright/test";

test("metrics", async ({ page }) => {
  await page.goto("/metrics");

  // Metrics page lazy-loads recharts — wait for the Suspense fallback to clear
  await page.waitForSelector(".loading", { state: "detached", timeout: 20_000 });
  await page.waitForLoadState("networkidle");

  // Extra pause for chart rendering (SVG paths settle after data resolves)
  await page.waitForTimeout(1000);

  await expect(page).toHaveScreenshot("metrics.png", { fullPage: true });
});
