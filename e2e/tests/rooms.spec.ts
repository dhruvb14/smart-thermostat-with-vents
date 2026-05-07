import { test, expect } from "@playwright/test";

test("rooms list", async ({ page }) => {
  await page.goto("/rooms");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  await expect(page).toHaveScreenshot("rooms.png", { fullPage: true });
});

test("room detail — Living Room", async ({ page }) => {
  await page.goto("/rooms");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Open the Living Room detail card / modal
  await page.getByText("Living Room").first().click();
  await page.waitForLoadState("networkidle");

  // Do NOT use fullPage: true here — on mobile the sticky <nav> is duplicated at each
  // viewport-slice boundary during Playwright's fullPage stitching, causing persistent
  // pixel instability. The viewport captures the expanded room detail which is what we
  // care about. The countdown timer is also masked: it ticks every second (setInterval
  // at Rooms.tsx:751) and would flip at minute boundaries between stability-check frames.
  await expect(page).toHaveScreenshot("room-detail.png", {
    mask: [page.locator(".room-status-next-timer")],
  });
});
