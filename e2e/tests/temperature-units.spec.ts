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
 * Coverage discipline: every test below tags itself with a `// @covers:` line
 * naming the backend field(s) it round-trips. `temperature-fields.ts` is the
 * single source of truth for the field list, and
 * `backend/tests/test_temperature_field_parity.py` asserts every UI-writable
 * entry is mentioned in at least one `@covers:` here. Add a field, add a tag.
 */

const UNIT = (process.env.PLENUM_TEMP_UNIT ?? "F") as "F" | "C";
const isCelsius = UNIT === "C";

// Values chosen so the °C number is comfortable in the field's valid range
// and round-trips cleanly through the backend's 2dp °F storage. Each pair
// is { °F input when in F mode, °C input when in C mode }.
const MIN_SETPOINT = isCelsius ? "17" : "62";
const MAX_SETPOINT = isCelsius ? "26" : "78";
const DEADBAND = isCelsius ? "0.5" : "0.9";
const OVERSHOOT_DELTA = isCelsius ? "0.3" : "0.5";
const DEFAULT_TEMP = isCelsius ? "22" : "72";
const COOLING_LOCKOUT = isCelsius ? "12" : "54";
const SCHEDULE_TARGET = isCelsius ? "20" : "68";
const ROOM_SYS_TEMP = isCelsius ? "21" : "70";
const ROOM_TEMP_OFFSET = isCelsius ? "0.5" : "0.9";
// Ambient pre-cool/pre-heat deltas (Issue #248). 5°C → 9.0°F and 3°C → 5.4°F
// both round-trip cleanly through the backend's 2dp °F storage, and the
// widened deadband (3) clears any thermostat deadband set earlier in this file.
const AMBIENT_MIN_DIFF = isCelsius ? "5" : "5";
const AMBIENT_DEADBAND = isCelsius ? "3" : "3";

// The seeded thermostat global-setup registers first. Use its specific id to
// avoid strict-mode collisions with the second thermostat ("upstairs").
const TC_ID = "climate.downstairs_thermostat";

test.describe(`Temperature round-trip (PLENUM_TEMP_UNIT=${UNIT})`, () => {
  test("thermostat fields persist exactly as entered (#231)", async ({
    page,
  }) => {
    // @covers: default_temp, min_setpoint, max_setpoint, deadband, overshoot_delta, cooling_lockout_below_f
    await page.goto("/thermostats");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    // Target the specific downstairs card by its unique input id. Two
    // thermostats are seeded, so unscoped label/role lookups would hit
    // strict-mode violations. Use [id="…"] (attribute selector) rather
    // than `#…` (CSS id selector) because the entity_id embeds a dot
    // (climate.downstairs_thermostat), which `#` treats as a class
    // separator.
    const idSel = (suffix: string) => `[id="thermo-${TC_ID}-${suffix}"]`;
    const card = page
      .locator(idSel("name"))
      .locator("xpath=ancestor::*[contains(@class,'card')][1]");

    await page.locator(idSel("default_temp")).fill(DEFAULT_TEMP);
    await page.locator(idSel("min_setpoint")).fill(MIN_SETPOINT);
    await page.locator(idSel("max_setpoint")).fill(MAX_SETPOINT);
    await page.locator(idSel("deadband")).fill(DEADBAND);
    await page.locator(idSel("overshoot_delta")).fill(OVERSHOOT_DELTA);
    // cooling_lockout_below_f uses a different id-suffix convention (`-cooling-lockout`,
    // hyphenated) because it was added separately from the SAFETY_FIELDS loop.
    await page
      .locator(`[id="thermo-${TC_ID}-cooling-lockout"]`)
      .fill(COOLING_LOCKOUT);

    // Capture the PUT response — when this assertion fails the body text
    // surfaces the actual backend error (range/validation/etc) instead of
    // an opaque "Saved! never appeared" timeout.
    const putResponse = page.waitForResponse(
      (r) =>
        r.url().includes(`/api/thermostats/${TC_ID}`) &&
        r.request().method() === "PUT",
    );
    await card.getByRole("button", { name: "Save changes" }).click();
    const response = await putResponse;
    const responseText = await response.text();
    expect(
      response.status(),
      `PUT /api/thermostats/${TC_ID} failed: ${responseText}`,
    ).toBeLessThan(400);

    // Reload and read back. The fix means each input should show the same
    // value that was typed; the double-conversion bug would surface as a
    // value off by the °C↔°F conversion factor.
    await page.reload();
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    const readBack = async (suffix: string) =>
      parseFloat(
        await page.locator(`[id="thermo-${TC_ID}-${suffix}"]`).inputValue(),
      );

    expect(await readBack("default_temp")).toBeCloseTo(
      parseFloat(DEFAULT_TEMP),
      1,
    );
    expect(await readBack("min_setpoint")).toBeCloseTo(
      parseFloat(MIN_SETPOINT),
      1,
    );
    expect(await readBack("max_setpoint")).toBeCloseTo(
      parseFloat(MAX_SETPOINT),
      1,
    );
    expect(await readBack("deadband")).toBeCloseTo(parseFloat(DEADBAND), 1);
    expect(await readBack("overshoot_delta")).toBeCloseTo(
      parseFloat(OVERSHOOT_DELTA),
      1,
    );
    expect(await readBack("cooling-lockout")).toBeCloseTo(
      parseFloat(COOLING_LOCKOUT),
      1,
    );
  });

  test("room presence temp and offset persist exactly as entered (#231)", async ({
    page,
  }) => {
    // @covers: system_wide_temp, temp_offset
    await page.goto("/rooms");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    // Open the Living Room's settings modal. Scope to the room card — the
    // bare `getByRole("button", { name: /Settings/i })` also matches the
    // gear in the top nav (aria-label="Settings"), which is the wrong target.
    const openModal = async () => {
      await page
        .locator(".card")
        .filter({ hasText: "Living Room" })
        .first()
        .getByRole("button", { name: "Settings", exact: true })
        .click();
      const m = page.locator(".modal");
      await m.waitFor({ state: "visible", timeout: 10_000 });
      return m;
    };

    let modal = await openModal();
    await modal
      .getByLabel(/Presence-triggered temperature/i)
      .fill(ROOM_SYS_TEMP);
    await modal.getByLabel(/Temperature offset/i).fill(ROOM_TEMP_OFFSET);

    await modal.getByRole("button", { name: /Save changes/i }).click();
    await modal.waitFor({ state: "detached", timeout: 5_000 });

    // Reopen and verify both persisted values.
    modal = await openModal();
    const sysTempAfter = await modal
      .getByLabel(/Presence-triggered temperature/i)
      .inputValue();
    const offsetAfter = await modal
      .getByLabel(/Temperature offset/i)
      .inputValue();
    expect(parseFloat(sysTempAfter)).toBeCloseTo(parseFloat(ROOM_SYS_TEMP), 1);
    expect(parseFloat(offsetAfter)).toBeCloseTo(
      parseFloat(ROOM_TEMP_OFFSET),
      1,
    );
  });

  test("room pre-cool/pre-heat deltas persist exactly as entered (#231)", async ({
    page,
  }) => {
    // @covers: ambient_suppression_min_differential, ambient_suppression_deadband
    // The pre-cool/pre-heat controls only render once an outside temperature
    // sensor is configured. Configure one via the API; skip on the no-HA stack
    // where the sensor entity does not exist (the conversion matrix that
    // actually exercises this runs against real HA).
    const res = await page.request.put("/api/settings/outside-temp-entity", {
      data: { entity_id: "sensor.outdoor_temperature" },
    });
    test.skip(
      !res.ok(),
      "requires an outside temperature sensor (HA-backed stack)",
    );

    await page.goto("/rooms");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    const openModal = async () => {
      await page
        .locator(".card")
        .filter({ hasText: "Living Room" })
        .first()
        .getByRole("button", { name: "Settings", exact: true })
        .click();
      const m = page.locator(".modal");
      await m.waitFor({ state: "visible", timeout: 10_000 });
      return m;
    };

    let modal = await openModal();
    // Enable the feature (the checkbox is enabled once the outside sensor loads),
    // then fill the two delta inputs that appear.
    await modal.getByLabel(/pre-cool \/ pre-heat/i).check();
    await modal
      .getByLabel(/Minimum outside difference/i)
      .fill(AMBIENT_MIN_DIFF);
    await modal.getByLabel(/Widened deadband/i).fill(AMBIENT_DEADBAND);

    await modal.getByRole("button", { name: /Save changes/i }).click();
    await modal.waitFor({ state: "detached", timeout: 5_000 });

    // Reopen and verify both deltas survived the °C↔°F round-trip unchanged.
    modal = await openModal();
    const minDiffAfter = await modal
      .getByLabel(/Minimum outside difference/i)
      .inputValue();
    const deadbandAfter = await modal
      .getByLabel(/Widened deadband/i)
      .inputValue();
    expect(parseFloat(minDiffAfter)).toBeCloseTo(
      parseFloat(AMBIENT_MIN_DIFF),
      1,
    );
    expect(parseFloat(deadbandAfter)).toBeCloseTo(
      parseFloat(AMBIENT_DEADBAND),
      1,
    );
  });

  test("schedule target temperature persists exactly as entered (#231)", async ({
    page,
  }) => {
    // @covers: target_temp
    await page.goto("/schedules");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    // Room cards on /schedules are collapsed by default — click the room's
    // title row to expand it, then the "+ Add schedule block" button appears.
    const livingRoomCard = page
      .locator(".card")
      .filter({ hasText: "Living Room" })
      .first();
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
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    const livingRoomCardReloaded = page
      .locator(".card")
      .filter({ hasText: "Living Room" })
      .first();
    await livingRoomCardReloaded.getByText("Living Room").click();

    const newBlockRow = livingRoomCardReloaded
      .locator("tr")
      .filter({ hasText: "18:00" });
    await expect(newBlockRow).toBeVisible();
    const text = await newBlockRow.innerText();
    const match = text.match(/(\d+(?:\.\d+)?)\s*°[CF]/);
    expect(match, `target temp not found in row: ${text}`).not.toBeNull();
    expect(parseFloat(match![1])).toBeCloseTo(parseFloat(SCHEDULE_TARGET), 1);
  });
});
