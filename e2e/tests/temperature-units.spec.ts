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

// Values chosen so the °C number is comfortable in the field's valid range
// and round-trips cleanly through the backend's 2dp °F storage.
const MIN_SETPOINT = isCelsius ? "17" : "62";
const MAX_SETPOINT = isCelsius ? "26" : "78";
const DEADBAND = isCelsius ? "0.5" : "0.9";
const SCHEDULE_TARGET = isCelsius ? "20" : "68";
const ROOM_SYS_TEMP = isCelsius ? "21" : "70";

// The seeded thermostat global-setup registers first. Use its specific id to
// avoid strict-mode collisions with the second thermostat ("upstairs").
const TC_ID = "climate.downstairs_thermostat";

test.describe(`Temperature round-trip (PLENUM_TEMP_UNIT=${UNIT})`, () => {
  test("thermostat min/max setpoint and deadband persist exactly as entered (#231)", async ({
    page,
  }) => {
    await page.goto("/thermostats");
    await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
    await page.waitForLoadState("networkidle");

    // Target the specific downstairs card by its unique input id. Two
    // thermostats are seeded, so unscoped label/role lookups would hit
    // strict-mode violations. Use [id="…"] (attribute selector) rather
    // than `#…` (CSS id selector) because the entity_id embeds a dot
    // (climate.downstairs_thermostat), which `#` treats as a class
    // separator.
    const idSel = (suffix: string) => `[id="thermo-${TC_ID}-${suffix}"]`;
    const minInput = page.locator(idSel("min_setpoint"));
    const maxInput = page.locator(idSel("max_setpoint"));
    const deadbandInput = page.locator(idSel("deadband"));
    const card = page
      .locator(idSel("name"))
      .locator("xpath=ancestor::*[contains(@class,'card')][1]");

    await minInput.fill(MIN_SETPOINT);
    await maxInput.fill(MAX_SETPOINT);
    await deadbandInput.fill(DEADBAND);

    // Capture the PUT response — when this assertion fails the body text
    // surfaces the actual backend error (range/validation/etc) instead of
    // an opaque "Saved! never appeared" timeout.
    const putResponse = page.waitForResponse(
      (r) =>
        r.url().includes(`/api/thermostats/${TC_ID}`) && r.request().method() === "PUT"
    );
    await card.getByRole("button", { name: "Save changes" }).click();
    const response = await putResponse;
    const responseText = await response.text();
    expect(response.status(), `PUT /api/thermostats/${TC_ID} failed: ${responseText}`).toBeLessThan(
      400
    );

    // Reload and read back. The fix means the input should show the same value
    // that was typed; the double-conversion bug would surface as a value off by
    // the °C↔°F conversion factor.
    await page.reload();
    await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
    await page.waitForLoadState("networkidle");

    expect(parseFloat(await page.locator(idSel("min_setpoint")).inputValue())).toBeCloseTo(
      parseFloat(MIN_SETPOINT),
      1
    );
    expect(parseFloat(await page.locator(idSel("max_setpoint")).inputValue())).toBeCloseTo(
      parseFloat(MAX_SETPOINT),
      1
    );
    expect(parseFloat(await page.locator(idSel("deadband")).inputValue())).toBeCloseTo(
      parseFloat(DEADBAND),
      1
    );
  });

  test("room presence-triggered temperature persists exactly as entered (#231)", async ({
    page,
  }) => {
    await page.goto("/rooms");
    await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
    await page.waitForLoadState("networkidle");

    // Open the Living Room's settings modal. Scope to the room card — the
    // bare `getByRole("button", { name: /Settings/i })` also matches the
    // gear in the top nav (aria-label="Settings"), which is the wrong target.
    const livingRoomCard = page.locator(".card").filter({ hasText: "Living Room" }).first();
    await livingRoomCard.getByRole("button", { name: "Settings", exact: true }).click();

    const modal = page.locator(".modal");
    await modal.waitFor({ state: "visible", timeout: 10_000 });

    const sysTempInput = modal.getByLabel(/Presence-triggered temperature/i);
    await sysTempInput.fill(ROOM_SYS_TEMP);

    await modal.getByRole("button", { name: /Save changes/i }).click();
    await modal.waitFor({ state: "detached", timeout: 5_000 });

    // Reopen the modal and verify the persisted value.
    await page
      .locator(".card")
      .filter({ hasText: "Living Room" })
      .first()
      .getByRole("button", { name: "Settings", exact: true })
      .click();
    const modal2 = page.locator(".modal");
    await modal2.waitFor({ state: "visible", timeout: 10_000 });
    const sysTempAfter = await modal2.getByLabel(/Presence-triggered temperature/i).inputValue();
    expect(parseFloat(sysTempAfter)).toBeCloseTo(parseFloat(ROOM_SYS_TEMP), 1);
  });

  test("schedule target temperature persists exactly as entered (#231)", async ({ page }) => {
    await page.goto("/schedules");
    await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
    await page.waitForLoadState("networkidle");

    // Room cards on /schedules are collapsed by default — click the room's
    // title row to expand it, then the "+ Add schedule block" button appears.
    const livingRoomCard = page.locator(".card").filter({ hasText: "Living Room" }).first();
    await livingRoomCard.getByText("Living Room").click();

    await livingRoomCard.getByText("+ Add schedule block").click();

    const modal = page.locator(".modal");
    await modal.waitFor({ state: "visible", timeout: 10_000 });

    // global-setup seeds a Mon-Fri 08:00-17:00 schedule on Living Room.
    // Pick an evening slot so the client-side overlap check doesn't reject
    // the save — overlap would leave the modal open and time us out.
    await modal.getByLabel(/Start time/i).fill("18:00");
    await modal.getByLabel(/End time/i).fill("20:00");
    await modal.getByLabel(/Target temperature/i).fill(SCHEDULE_TARGET);

    await modal.getByRole("button", { name: /^Save$/ }).click();
    await modal.waitFor({ state: "detached", timeout: 5_000 });

    // Reload and verify the new block is present with the value the user
    // typed. The schedules table renders the target via fmtTemp(°F) which
    // re-derives the display unit from storage; if the backend stored the
    // wrong value the rendered number would be off.
    await page.reload();
    await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
    await page.waitForLoadState("networkidle");

    const livingRoomCardReloaded = page.locator(".card").filter({ hasText: "Living Room" }).first();
    await livingRoomCardReloaded.getByText("Living Room").click();

    const newBlockRow = livingRoomCardReloaded.locator("tr").filter({ hasText: "18:00" });
    await expect(newBlockRow).toBeVisible();
    const text = await newBlockRow.innerText();
    const match = text.match(/(\d+(?:\.\d+)?)\s*°[CF]/);
    expect(match, `target temp not found in row: ${text}`).not.toBeNull();
    expect(parseFloat(match![1])).toBeCloseTo(parseFloat(SCHEDULE_TARGET), 1);
  });
});
