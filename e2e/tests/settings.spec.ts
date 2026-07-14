import { test, expect } from "./fixtures";

// The dedicated Settings page (#471): the MCP server toggle + inline setup
// guidance and Backup & Restore. (The MCP access-token card only renders when
// require_auth is on — that's the @auth leg, captured by auth.spec.ts — so it
// is absent here.) This replaced the old capture that screenshotted the
// per-thermostat settings panel under the same `settings.png` name.
test("settings page", async ({ page }) => {
  await page.goto("/settings");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");
  await expect(page.getByText("MCP server")).toBeVisible();

  await expect(page).toHaveScreenshot("settings.png", { fullPage: true });
});
