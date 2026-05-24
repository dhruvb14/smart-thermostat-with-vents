import { test, expect } from "./fixtures";

/**
 * Round-trip regression for the temperature-unit conversion contract (#231).
 *
 * The bug this exercises: in Celsius mode, the frontend used to convert °C →
 * °F via `toStorage` before POSTing, AND the backend's `_to_f` converted again
 * — so a user typing "16" (°C) ended up with 141.44°F persisted instead of
 * 60.8°F. The fix pushed conversion entirely to the backend (Option A in the
 * issue): the frontend sends display-unit values as-is.
 *
 * The double-conversion was invisible to per-side unit tests (frontend tests
 * asserted "POSTs the pre-converted °F"; backend tests asserted "GET returns
 * °F when given °C") — only an end-to-end round-trip exposes it.
 *
 * This spec is parameterised on PLENUM_TEMP_UNIT (set by the CI matrix) so
 * the same assertions run under both °F and °C stacks. Under °F the round-trip
 * is trivial (no conversion); under °C it is the regression test.
 *
 * Each test edits a temperature field via the UI, reloads, and verifies the
 * value the field shows equals the value the user typed. If conversion is
 * compounded or skipped, the round-trip fails.
 */

const UNIT = (process.env.PLENUM_TEMP_UNIT ?? "F") as "F" | "C";
const isCelsius = UNIT === "C";
const unitLabel = isCelsius ? "°C" : "°F";

// Values chosen so the °C number is comfortable in the field's valid range
// and round-trips cleanly through the backend's 2dp °F storage.
const MIN_SETPOINT = isCelsius ? "17" : "62";
const MAX_SETPOINT = isCelsius ? "26" : "78";
const DEADBAND = isCelsius ? "0.5" : "0.9";
const SCHEDULE_TARGET = isCelsius ? "20" : "68";
const ROOM_SYS_TEMP = isCelsius ? "21" : "70";

test.describe(`Temperature round-trip (PLENUM_TEMP_UNIT=${UNIT})`, () => {
  test("thermostat min/max setpoint and deadband persist exactly as entered (#231)", async ({
    page,
  }) => {
    await page.goto("/thermostats");
    await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
    await page.waitForLoadState("networkidle");

    // First thermostat card — global-setup registers downstairs + upstairs.
    const minInput = page.locator('[id$="-min_setpoint"]').first();
    const maxInput = page.locator('[id$="-max_setpoint"]').first();
    const deadbandInput = page.locator('[id$="-deadband"]').first();

    // Sanity: the label reflects the active unit.
    await expect(page.locator("label").filter({ hasText: `Min setpoint (${unitLabel})` })).toBeVisible();

    await minInput.fill(MIN_SETPOINT);
    await maxInput.fill(MAX_SETPOINT);
    await deadbandInput.fill(DEADBAND);

    // Save the first card. There's one "Save changes" button per card.
    await page.getByRole("button", { name: "Save changes" }).first().click();
    await expect(page.getByText("Saved!").first()).toBeVisible({ timeout: 5_000 });

    // Reload and read back. The fix means the input should show the same value
    // that was typed; the double-conversion bug would surface as a value off by
    // the °C↔°F conversion factor.
    await page.reload();
    await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
    await page.waitForLoadState("networkidle");

    const minAfter = await page.locator('[id$="-min_setpoint"]').first().inputValue();
    const maxAfter = await page.locator('[id$="-max_setpoint"]').first().inputValue();
    const deadbandAfter = await page.locator('[id$="-deadband"]').first().inputValue();

    expect(parseFloat(minAfter)).toBeCloseTo(parseFloat(MIN_SETPOINT), 1);
    expect(parseFloat(maxAfter)).toBeCloseTo(parseFloat(MAX_SETPOINT), 1);
    expect(parseFloat(deadbandAfter)).toBeCloseTo(parseFloat(DEADBAND), 1);
  });

  test("room presence-triggered temperature persists exactly as entered (#231)", async ({
    page,
  }) => {
    await page.goto("/rooms");
    await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
    await page.waitForLoadState("networkidle");

    // Open the first room's settings modal.
    await page.getByRole("button", { name: /Settings/i }).first().click();
    const sysTempInput = page.getByLabel(/Presence-triggered temperature/i);
    await sysTempInput.fill(ROOM_SYS_TEMP);

    await page.getByRole("button", { name: /Save changes/i }).click();

    // Modal closes on success; reopen to verify the persisted value.
    await page.waitForSelector(".modal", { state: "detached", timeout: 5_000 });
    await page.getByRole("button", { name: /Settings/i }).first().click();
    const sysTempAfter = await page.getByLabel(/Presence-triggered temperature/i).inputValue();
    expect(parseFloat(sysTempAfter)).toBeCloseTo(parseFloat(ROOM_SYS_TEMP), 1);
  });

  test("schedule target temperature persists exactly as entered (#231)", async ({ page }) => {
    await page.goto("/schedules");
    await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
    await page.waitForLoadState("networkidle");

    // Pick the first room with at least one schedule block, open editor on it.
    // global-setup creates schedules for the seeded rooms; "+ Add schedule block"
    // is always present whichever room is selected.
    await page.locator(".room-tab, .room-card, [data-room-id]").first().click().catch(() => {});
    await page.getByText("+ Add schedule block").first().click();

    await page.getByLabel(/Start time/i).fill("13:00");
    await page.getByLabel(/End time/i).fill("15:00");
    await page.getByLabel(/Target temperature/i).fill(SCHEDULE_TARGET);

    await page.getByRole("button", { name: /^Save$/ }).click();
    // Modal closes; the new block appears in the list. Reload to be sure the
    // backend persisted the right value rather than just the in-memory one.
    await page.waitForSelector(".modal", { state: "detached", timeout: 5_000 });
    await page.reload();
    await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
    await page.waitForLoadState("networkidle");

    // The schedule list renders the target as e.g. "20.0°C" — read it back and
    // check the numeric portion. Locate the most recent block by its 13:00 start.
    const block = page.locator("text=13:00").first();
    await expect(block).toBeVisible();
    const blockCard = block.locator("xpath=ancestor::*[contains(@class,'schedule')][1]");
    const text = await blockCard.innerText();
    const match = text.match(/(\d+(?:\.\d+)?)\s*°[CF]/);
    expect(match, `target temp not found in schedule block text: ${text}`).not.toBeNull();
    expect(parseFloat(match![1])).toBeCloseTo(parseFloat(SCHEDULE_TARGET), 1);
  });
});
