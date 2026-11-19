import {
  test,
  expect,
  settleForCapture,
  markDocument,
  expectSameDocument,
} from "./fixtures";

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
 * The second test covers a SCHEDULED trip (#728): it schedules one far in the
 * future through the same dialog, checks the blue scheduled banner and the
 * stored `state`, captures the banner, then cancels the trip through the
 * confirm step and checks that no welcome-home banner appears. It starts by
 * ending the first test's active trip without welcome home, and the shared
 * cleanup does the same, so no welcome-home window can leak into another
 * spec's full-page goldens.
 *
 * The "Returning …" timestamp uses a fixed, far-future return time, typed into
 * the picker, so the rendered text is stable across passes — no <Frozen>
 * needed. The picker takes and the banner shows the HOUSE's zone (#729), so
 * the typed wall clock comes back verbatim whatever zone the browser is in.
 */

const API = `${process.env.PLENUM_URL ?? "http://localhost:8099"}/api`;

// What the user types into the datetime-local picker (house time), and the
// text it must come back as.
const RETURN_AT_INPUT = "2030-06-01T10:00";
const RETURN_AT_LOCAL = "6/1/2030, 10:00:00 AM";

// A scheduled trip (#728), far enough out that the two-hour heads-up never
// shows, so the banner text is the same on every pass.
const SCHEDULED_START_INPUT = "2099-03-01T09:00";
const SCHEDULED_START_LOCAL = "3/1/2099, 9:00:00 AM";
const SCHEDULED_RETURN_INPUT = "2099-03-08T18:00";
const SCHEDULED_RETURN_LOCAL = "3/8/2099, 6:00:00 PM";

// Ends (or cancels) any trip WITHOUT welcome home (#728): ending an active
// trip normally opens a welcome-home window, whose banner would then sit in
// every other spec's full-page goldens.
async function disableVacationMode(): Promise<void> {
  const res = await fetch(`${API}/settings/vacation-mode?welcome_home=false`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`disableVacationMode → ${res.status}`);
}

test.describe.serial("Vacation mode banner (#363, #678, #728)", () => {
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
    await modal.getByLabel(/Start immediately/i).check();
    await modal.getByLabel(/Return date/i).fill(RETURN_AT_INPUT);
    await modal.getByRole("button", { name: /Enable vacation mode/i }).click();
    await modal.waitFor({ state: "detached" });

    // The banner appears in the same document — no reload, no navigation.
    await expect(banner).toBeVisible();
    await expect(banner).toContainText("Vacation mode active");
    await expect(banner).toContainText(`Returning ${RETURN_AT_LOCAL}.`);
    await expectSameDocument(page);
    // …and the Dashboard's own control agrees with it.
    await expect(
      page.getByRole("button", { name: /Vacation mode active/i }),
    ).toBeVisible();

    // Back to the at-rest pose before any capture: the clicks above may have
    // scrolled the banner under the sticky nav and left a button hovered.
    await settleForCapture(page);

    // Element-scoped shot of just the banner.
    await expect(banner).toHaveScreenshot("vacation-mode-banner.png");

    // Full-page shot of the Dashboard so the banner is visible in context —
    // where it renders relative to the page title and the rest of the page.
    // The volatile bits (the "Updated HH:MM:SS" clock, live cycle state /
    // active-room count / progress bar) are frozen by the isCI flag under the
    // CI build, same as dashboard.spec.ts. See issue #182.
    await expect(page).toHaveScreenshot("vacation-mode-dashboard.png", {
      fullPage: true,
    });
  });
  test("scheduling a trip shows the scheduled banner, and cancelling removes it (#728)", async ({
    page,
  }) => {
    // The first test leaves a trip active; start this one from "off".
    await disableVacationMode();
    await page.goto("/");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    const scheduled = page.getByTestId("vacation-scheduled-banner");
    await expect(scheduled).toHaveCount(0);

    // The modal asks for a start by default; far-future dates because the
    // backend checks "in the future" against its real clock.
    await page.getByRole("button", { name: /Enable vacation mode/i }).click();
    const modal = page.locator(".modal");
    await modal.getByLabel(/Start date/i).fill(SCHEDULED_START_INPUT);
    await modal.getByLabel(/Return date/i).fill(SCHEDULED_RETURN_INPUT);
    await modal.getByRole("button", { name: /Schedule vacation/i }).click();
    await modal.waitFor({ state: "detached" });

    await expect(scheduled).toBeVisible();
    await expect(scheduled).toContainText(
      `Starts ${SCHEDULED_START_LOCAL}, returns ${SCHEDULED_RETURN_LOCAL}. Until then everything runs as normal.`,
    );
    // Nothing is paused yet: no active banner, and the Dashboard says so.
    await expect(page.getByTestId("vacation-mode-banner")).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: /Vacation scheduled/i }),
    ).toBeVisible();
    const stored = (await (
      await fetch(`${API}/settings/vacation-mode`)
    ).json()) as { state: string; enabled: boolean };
    expect(stored).toMatchObject({ state: "scheduled", enabled: false });

    await settleForCapture(page);
    await expect(scheduled).toHaveScreenshot("vacation-scheduled-banner.png");

    // Cancel it from the banner's Manage button, through the confirm step.
    await scheduled.getByRole("button", { name: /Manage/i }).click();
    await modal
      .getByRole("button", { name: /Cancel scheduled vacation/i })
      .click();
    await modal.getByRole("button", { name: /Yes, cancel it/i }).click();
    await modal.waitFor({ state: "detached" });

    await expect(scheduled).toHaveCount(0);
    // Cancelling a trip that never started fires no welcome home.
    await expect(page.getByTestId("welcome-home-banner")).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: /Enable vacation mode/i }),
    ).toBeVisible();
  });
});
