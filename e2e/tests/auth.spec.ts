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
  // The MCP token card moved to the dedicated Settings page (#471).
  await page.goto("/settings");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  // Screenshot just the card locator, so we wait for the card itself.
  const card = page.locator(".card").filter({ hasText: "MCP access tokens" });
  await expect(card).toBeVisible({ timeout: 15_000 });
  await expect(card.getByText(/No tokens yet/i)).toBeVisible();
  // Empty state — the mint form (label + scope select + help text) is the
  // primary control; list/revoke rendering is covered by the vitest unit test.
  await expect(card).toHaveScreenshot("mcp-tokens-card.png");
});

test("@auth MCP token revoke confirmation dialog", async ({ page }) => {
  await page.goto("/settings");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  const card = page.locator(".card").filter({ hasText: "MCP access tokens" });
  await expect(card).toBeVisible({ timeout: 15_000 });

  // Mint a token so there's something to revoke.
  await card.getByLabel(/Label/i).fill("E2E revoke test");
  await card.getByRole("button", { name: "Mint token" }).click();
  await expect(card.getByText(/won't be shown again/i)).toBeVisible();
  await card.getByRole("button", { name: "Done" }).click();

  await card.getByRole("button", { name: "Revoke" }).click();
  const dialog = page.getByTestId("confirm-dialog");
  await expect(dialog).toBeVisible();
  // Scope to the message <p>, not the whole dialog — matches the pattern
  // used for the other confirm-dialog specs (see rooms.spec.ts).
  await expect(dialog.locator("p")).toContainText("E2E revoke test");

  await expect(page).toHaveScreenshot("mcp-token-revoke-confirm.png", { maxDiffPixels: 800 });

  // Confirm the revoke (rather than cancel) so the token store is back to
  // empty afterward — the sibling "MCP token management card" test asserts
  // the empty state and must not see a leftover token, regardless of order.
  await dialog.getByRole("button", { name: "Revoke" }).click();
  await expect(card.getByText(/No tokens yet/i)).toBeVisible();
});

test("@auth settings page (auth on — full page with token card)", async ({ page }) => {
  // The non-auth F/C legs capture /settings with require_auth=false, so their
  // `settings.png` shows the MCP server card + Backup only (the token card is
  // gated on require_auth). This is the complementary capture WITH auth on:
  // the full page must show the MCP server toggle, the MCP access-tokens
  // minting card directly beneath it, and Backup & Restore — the layout a real
  // authenticated install sees. Distinct filename so it never collides with the
  // auth-off `settings.png`.
  await page.goto("/settings");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");
  // Both MCP controls present, proving the toggle and the minting UI sit together.
  await expect(page.getByText("MCP server", { exact: true })).toBeVisible();
  await expect(page.getByText("MCP access tokens")).toBeVisible();
  await expect(page).toHaveScreenshot("settings-auth.png", { fullPage: true });
});
