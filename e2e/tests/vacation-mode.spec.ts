import { test, expect, settleForFullPageShot, markDocument, expectSameDocument } from "./fixtures";

/**
 * Visual coverage of the global "Vacation mode active" banner (Issue #363),
 * reached the way a user reaches it — through the Dashboard UI (#678).
 *
 * The banner is mounted once in App.tsx above <main>, so it appears above the
 * page title on every route. It is styled as a card with a green left border —
 * visually parallel to the amber StaleSensorsBanner but green to signal an
 * informational/active state.
 *
 * This spec enables vacation mode by clicking through the Dashboard's own
 * control and modal, then asserts the banner renders in the SAME document — no
 * reload. That is the #678 regression: the banner used to read its state only
 * on mount, so the click path left it missing until a full reload (a
 * force-close in the PWA). Seeding the state through the API before page load,
 * as this spec originally did, could never catch that.
 *
 * Determinism on the shared stack: vacation mode is global backend state, so —
 * like schedule-flow.spec.ts resets the rooms it touches — beforeAll makes sure
 * it is off (the Dashboard only offers "Enable" from that state) and afterAll
 * turns it off again. That keeps it off for every other spec's full-page goldens
 * (which would otherwise capture the banner), and keeps every project and the
 * update→verify double pass starting from the same state.
 *
 * The "Returning …" timestamp uses a fixed, far-future return time, typed into
 * the picker, so the rendered text is stable across passes — no <Frozen>
 * needed. Playwright pins the browser to UTC, so the typed local time IS the
 * stored instant.
 */

const API = `${process.env.PLENUM_URL ?? "http://localhost:8099"}/api`;

// What the user types into the datetime-local picker (browser TZ is UTC), and
// the instant/text it must come back as.
const RETURN_AT_INPUT = "2030-06-01T10:00";
const RETURN_AT_LOCAL = "6/1/2030, 10:00:00 AM";

async function disableVacationMode(): Promise<void> {
  const res = await fetch(`${API}/settings/vacation-mode`, { method: "DELETE" });
  if (!res.ok) throw new Error(`disableVacationMode → ${res.status}`);
}

test.describe.serial("Vacation mode banner (#363, #678)", () => {
  test.beforeAll(disableVacationMode);
  test.afterAll(disableVacationMode);

  test("enabling vacation mode from the Dashboard shows the banner without a reload", async ({
    page,
  }) => {
    await page.goto("/");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    const banner = page.getByTestId("vacation-mode-banner");
    await expect(banner).toHaveCount(0);
    await markDocument(page);

    // Enable it the way a user does: the Dashboard button, then the modal.
    await page.getByRole("button", { name: /Enable vacation mode/i }).click();
    const modal = page.locator(".modal");
    await modal.getByLabel(/Return date/i).fill(RETURN_AT_INPUT);
    await modal.getByRole("button", { name: /Enable vacation mode/i }).click();
    await modal.waitFor({ state: "detached" });

    // The banner appears in the same document — no reload, no navigation.
    await expect(banner).toBeVisible();
    await expect(banner).toContainText("Vacation mode active");
    await expect(banner).toContainText(`Returning ${RETURN_AT_LOCAL}.`);
    await expectSameDocument(page);
    // …and the Dashboard's own control agrees with it.
    await expect(page.getByRole("button", { name: /Vacation mode active/i })).toBeVisible();

    // Element-scoped shot of just the banner.
    await expect(banner).toHaveScreenshot("vacation-mode-banner.png");

    // Full-page shot of the Dashboard so the banner is visible in context —
    // where it renders relative to the page title and the rest of the page.
    // The volatile bits (the "Updated HH:MM:SS" clock, live cycle state /
    // active-room count / progress bar) are frozen by the isCI flag under the
    // CI build, same as dashboard.spec.ts. See issue #182.
    await settleForFullPageShot(page);
    await expect(page).toHaveScreenshot("vacation-mode-dashboard.png", {
      fullPage: true,
    });
  });
});
