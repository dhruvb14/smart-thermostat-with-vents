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

  await expect(page).toHaveScreenshot("room-detail.png", { fullPage: true });
});
