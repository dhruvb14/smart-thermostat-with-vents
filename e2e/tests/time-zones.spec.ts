import { test, expect } from "./fixtures";

/**
 * House time zone, end to end (Issue #729).
 *
 * Every time Plenum shows or accepts is in the HOUSE's zone (the add-on's
 * `timezone` option; the test stack sets `TIMEZONE: America/New_York`), never
 * the browser's. This spec views Plenum from a browser in Asia/Tokyo — 13 or
 * 14 hours away from the house — and proves the whole chain:
 *
 *  1. the Settings page names the house zone;
 *  2. a time typed into a picker is read as house wall-clock time, so the API
 *     stores the New York instant, not the Tokyo one;
 *  3. the UI shows that instant back as the same wall clock, with the zone's
 *     short name (EST in January, EDT in July) as its tooltip, and still does
 *     after a reload.
 *
 * Vacation mode is the vehicle because its return time round-trips through
 * one global setting the spec can read back over the API. The spec takes no
 * screenshots, and beforeAll/afterAll turn vacation mode off, so every other
 * spec's goldens and the update→verify double pass start from the same state
 * (the same reset vacation-mode.spec.ts uses). The return times are far in the
 * future because the backend validates "in the future" against its real
 * clock, while the browser clock is pinned to 2025 (fixtures.ts).
 */

const API = `${process.env.PLENUM_URL ?? "http://localhost:8099"}/api`;

test.use({ timezoneId: "Asia/Tokyo" });

// Ends (or cancels) any trip WITHOUT welcome home (#728): ending an active
// trip normally opens a welcome-home window, whose banner would then sit in
// every other spec's full-page goldens.
async function disableVacationMode(): Promise<void> {
  const res = await fetch(`${API}/settings/vacation-mode?welcome_home=false`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`disableVacationMode → ${res.status}`);
}

async function storedReturnAt(): Promise<number> {
  const res = await fetch(`${API}/settings/vacation-mode`);
  if (!res.ok) throw new Error(`GET vacation-mode → ${res.status}`);
  const body = (await res.json()) as { return_at: string | null };
  if (!body.return_at) throw new Error("vacation mode has no return_at");
  return Date.parse(body.return_at);
}

// One winter and one summer case, so both sides of New York's DST are pinned.
const CASES = [
  {
    name: "winter (EST, UTC-5)",
    input: "2099-01-15T10:00",
    instant: Date.UTC(2099, 0, 15, 15, 0),
    shown: "1/15/2099, 10:00:00 AM",
    short: "EST",
  },
  {
    name: "summer (EDT, UTC-4)",
    input: "2099-07-15T10:00",
    instant: Date.UTC(2099, 6, 15, 14, 0),
    shown: "7/15/2099, 10:00:00 AM",
    short: "EDT",
  },
] as const;

test.describe.serial("House time zone (#729)", () => {
  test.beforeAll(disableVacationMode);
  test.afterAll(disableVacationMode);

  test("Settings names the house zone, not the browser's", async ({ page }) => {
    await page.goto("/settings");
    const card = page.getByTestId("timezone-card");
    await expect(card).toContainText("America/New_York");
    await expect(card).not.toContainText("Asia/Tokyo");
    // The browser clock is pinned to 2025-06-04, so "currently" is summer time.
    await expect(card).toContainText("(currently EDT)");
    // The OS-fallback warning only shows when the option is missing/invalid.
    await expect(card.locator(".badge")).toHaveCount(0);
  });

  for (const c of CASES) {
    test(`a typed time is house time — ${c.name}`, async ({ page }) => {
      await disableVacationMode();
      await page.goto("/");
      await page.waitForSelector(".loading", {
        state: "detached",
        timeout: 15_000,
      });
      await page.waitForLoadState("networkidle");

      await page.getByRole("button", { name: /Enable vacation mode/i }).click();
      const modal = page.locator(".modal");
      // Each picker says which zone it is typed in before anything is typed.
      // The dialog opens with both the start and the return picker (#728).
      await expect(
        modal.getByRole("button", { name: /Times are in E[SD]T/ }),
      ).toHaveCount(2);
      await modal.getByLabel(/Start immediately/i).check();
      await modal.getByLabel(/Return date/i).fill(c.input);
      await modal
        .getByRole("button", { name: /Enable vacation mode/i })
        .click();
      await modal.waitFor({ state: "detached" });

      // The API stored the New York instant — read in Tokyo it would be 14
      // hours earlier.
      expect(await storedReturnAt()).toBe(c.instant);

      // …and the UI shows the same wall clock back, labelled with the zone.
      const banner = page.getByTestId("vacation-mode-banner");
      const returning = banner.getByText(/Returning/);
      await expect(returning).toContainText(`Returning ${c.shown}.`);
      await expect(returning).toHaveAttribute("title", c.short);

      // Still the same after a reload (the zone is read from the server).
      await page.reload();
      await expect(
        page.getByTestId("vacation-mode-banner").getByText(/Returning/),
      ).toContainText(`Returning ${c.shown}.`);
    });
  }
});
