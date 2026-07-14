import { test, expect } from "./fixtures";

// Visual-regression coverage for the #373 authentication UI. These run ONLY on
// the auth leg (docker-compose.test.auth.yml → require_auth=true); the `@auth`
// tag excludes them from the normal F/C legs, where require_auth is false and
// this UI does not render. The fixtures auto-inject a valid session cookie, so
// the app is authenticated by default; the login test clears it to capture the
// unauthenticated screen.

test("@auth login screen (unauthenticated direct-port access)", async ({ page, context }) => {
  // Drop the injected session so /api/auth/status reports unauthenticated and
  // the SPA renders the login gate instead of the app.
  await context.clearCookies();
  await page.goto("/");
  await expect(page.getByRole("button", { name: /^Sign in$/i })).toBeVisible();
  await expect(page.getByLabel(/Username/i)).toBeVisible();
  await expect(page.getByLabel(/Password/i)).toBeVisible();
  await expect(page).toHaveScreenshot("login.png", { fullPage: true });
});

test("@auth settings menu shows auth status + logout", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");
  await page.getByRole("button", { name: "Settings" }).click();
  // The session-authenticated status line + Log out item are the new controls.
  await expect(page.getByText(/Signed in \(direct access\)/i)).toBeVisible();
  await expect(page.getByText(/Log out/i)).toBeVisible();
  await expect(page.locator(".settings-menu")).toHaveScreenshot("settings-menu-auth.png");
});

test("@auth MCP token management card", async ({ page }) => {
  await page.goto("/thermostats");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  // Screenshot just the card locator, so we wait for the card itself rather than
  // networkidle (the Thermostats page keeps polling live state).
  const card = page.locator(".card").filter({ hasText: "MCP access tokens" });
  await expect(card).toBeVisible({ timeout: 15_000 });
  await expect(card.getByText(/No tokens yet/i)).toBeVisible();
  // Empty state — the mint form (label + scope select + help text) is the
  // primary control; list/revoke rendering is covered by the vitest unit test.
  await expect(card).toHaveScreenshot("mcp-tokens-card.png");
});
