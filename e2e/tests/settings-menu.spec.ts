import { test, expect } from "./fixtures";

// The open settings-cog dropdown (System / Dev / MCP toggles + the Theme
// cycler added with #458) had no golden before — capture it so menu changes
// are visible in PR diffs across both themes.
test("settings dropdown menu open", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  await page.getByRole("button", { name: "Settings" }).click();
  await expect(page.getByText("Theme:")).toBeVisible();

  await expect(page).toHaveScreenshot("settings-menu.png");
});
