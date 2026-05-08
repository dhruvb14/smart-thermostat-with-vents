import { test, expect } from "./fixtures";

test("rooms list", async ({ page }) => {
  await page.goto("/rooms");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  await expect(page).toHaveScreenshot("rooms.png", {
    fullPage: true,
    // Mask the full status row and live temp: the timer text (e.g. "7h 5m")
    // changes width each run, which shifts the pink mask bounds and causes
    // spurious diffs even when nothing else changed. Using the row-level
    // element gives a fixed-width bounding box that never varies.
    mask: [
      page.locator(".room-status-row"),
      page.locator(".room-live-value"),
    ],
  });
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
    // Mask the whole status row rather than individual sub-elements: the timer
    // span (.room-status-next-timer) changes text width each run, which resizes
    // its pink mask box and causes spurious diffs. The row itself spans the full
    // card width so its bounding box is stable regardless of content.
    mask: [
      page.locator(".room-status-row"),
      page.locator(".room-live-value"),
    ],
  });
});
