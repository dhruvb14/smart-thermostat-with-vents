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
// Schedule deadband override (Issue #517). A DELTA, so it scales by 9/5 with
// no +32 offset: 1°C ≡ 1.8°F, which round-trips exactly through 2dp storage.
const SCHEDULE_DRIFT = isCelsius ? "1" : "1.8";
const ROOM_SYS_TEMP = isCelsius ? "21" : "70";
const ROOM_TEMP_OFFSET = isCelsius ? "0.5" : "0.9";
// Per-room deadband override (Issue #277). A delta, so it round-trips through
// the backend's 2dp °F storage cleanly at 0.5°C → 0.9°F.
const ROOM_DEADBAND_OVERRIDE = isCelsius ? "0.5" : "0.9";
// Ambient pre-cool/pre-heat deltas (Issue #248). 5°C → 9.0°F and 3°C → 5.4°F
// both round-trip cleanly through the backend's 2dp °F storage, and the
// widened deadband (3) clears any thermostat deadband set earlier in this file.
const AMBIENT_MIN_DIFF = isCelsius ? "5" : "5";
const AMBIENT_DEADBAND = isCelsius ? "3" : "3";

// Eco Mode (Issue #404). Outdoor thresholds / full-drift temps are absolute;
// max-drift and hysteresis are deltas. Every value round-trips cleanly through
// the backend's 2dp °F storage. Keyed by the field-name suffix used in the
// input ids so both the thermostat and room round-trips share one map. The
// numeric fields are editable without turning Eco on (only the toggle needs an
// outside sensor), so these round-trips do not configure one.
const ECO_VALUES: Record<string, string> = {
  eco_cooling_outdoor_threshold: isCelsius ? "31" : "88",
  eco_cooling_full_drift_temp: isCelsius ? "39" : "102",
  eco_cooling_max_drift: isCelsius ? "2" : "4",
  eco_heating_outdoor_threshold: isCelsius ? "3" : "38",
  eco_heating_full_drift_temp: isCelsius ? "-17" : "2",
  eco_heating_max_drift: isCelsius ? "2" : "4",
  eco_hysteresis_band: isCelsius ? "1" : "2",
};

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
    // @covers: system_wide_temp, temp_offset, deadband_override
    await page.goto("/rooms");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    // Open the Living Room's settings page. Scope to the room card — the
    // bare `getByRole("button", { name: /Settings/i })` also matches the
    // gear in the top nav (aria-label="Settings"), which is the wrong target.
    // Settings is now a full-page view (not a modal); target its container.
    const openSettings = async () => {
      await page
        .locator(".card")
        .filter({ hasText: "Living Room" })
        .first()
        .getByRole("button", { name: "Settings", exact: true })
        .click();
      const s = page.getByTestId("room-settings");
      await s.waitFor({ state: "visible", timeout: 10_000 });
      return s;
    };

    let settings = await openSettings();
    await settings
      .getByLabel(/Presence-triggered temperature/i)
      .fill(ROOM_SYS_TEMP);
    await settings.getByLabel(/Temperature offset/i).fill(ROOM_TEMP_OFFSET);
    await settings.getByLabel(/Deadband override/i).fill(ROOM_DEADBAND_OVERRIDE);

    await settings.getByRole("button", { name: /Save changes/i }).click();
    await settings.waitFor({ state: "detached", timeout: 5_000 });

    // Reopen and verify all persisted values.
    settings = await openSettings();
    const sysTempAfter = await settings
      .getByLabel(/Presence-triggered temperature/i)
      .inputValue();
    const offsetAfter = await settings
      .getByLabel(/Temperature offset/i)
      .inputValue();
    const deadbandOverrideAfter = await settings
      .getByLabel(/Deadband override/i)
      .inputValue();
    expect(parseFloat(sysTempAfter)).toBeCloseTo(parseFloat(ROOM_SYS_TEMP), 1);
    expect(parseFloat(offsetAfter)).toBeCloseTo(
      parseFloat(ROOM_TEMP_OFFSET),
      1,
    );
    expect(parseFloat(deadbandOverrideAfter)).toBeCloseTo(
      parseFloat(ROOM_DEADBAND_OVERRIDE),
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
    //
    // The outside-temp entity is a global setting, and later specs (e.g.
    // thermostats.spec.ts) screenshot the page that renders it — workers:1 runs
    // every spec sequentially against the same stack — so capture the prior
    // value and restore it in `finally` to avoid contaminating those goldens.
    const priorOutside = await (
      await page.request.get("/api/settings/outside-temp-entity")
    ).json();
    const res = await page.request.put("/api/settings/outside-temp-entity", {
      data: { entity_id: "sensor.outdoor_temperature" },
    });
    test.skip(
      !res.ok(),
      "requires an outside temperature sensor (HA-backed stack)",
    );

    try {
      await page.goto("/rooms");
      await page.waitForSelector(".loading", {
        state: "detached",
        timeout: 15_000,
      });
      await page.waitForLoadState("networkidle");

      const openSettings = async () => {
        await page
          .locator(".card")
          .filter({ hasText: "Living Room" })
          .first()
          .getByRole("button", { name: "Settings", exact: true })
          .click();
        const s = page.getByTestId("room-settings");
        await s.waitFor({ state: "visible", timeout: 10_000 });
        return s;
      };

      let settings = await openSettings();
      // Enable the feature (the checkbox is enabled once the outside sensor loads),
      // then fill the two delta inputs that appear.
      await settings.getByLabel(/pre-cool \/ pre-heat/i).check();
      await settings
        .getByLabel(/Minimum outside difference/i)
        .fill(AMBIENT_MIN_DIFF);
      await settings.getByLabel(/Widened deadband/i).fill(AMBIENT_DEADBAND);

      await settings.getByRole("button", { name: /Save changes/i }).click();
      await settings.waitFor({ state: "detached", timeout: 5_000 });

      // Reopen and verify both deltas survived the °C↔°F round-trip unchanged.
      settings = await openSettings();
      const minDiffAfter = await settings
        .getByLabel(/Minimum outside difference/i)
        .inputValue();
      const deadbandAfter = await settings
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
    } finally {
      // Restore the global outside-temp setting for subsequent specs.
      await page.request.put("/api/settings/outside-temp-entity", {
        data: { entity_id: priorOutside?.entity_id ?? null },
      });
    }
  });

  test("thermostat Eco Mode fields persist exactly as entered (#404)", async ({
    page,
  }) => {
    // @covers: eco_cooling_outdoor_threshold, eco_cooling_full_drift_temp, eco_cooling_max_drift, eco_heating_outdoor_threshold, eco_heating_full_drift_temp, eco_heating_max_drift, eco_hysteresis_band
    await page.goto("/thermostats");
    await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
    await page.waitForLoadState("networkidle");

    // The Eco numeric inputs are always editable (only the enable toggle needs
    // an outside sensor). Fill each via its unique input id, save, reload, and
    // assert the values survived the °C↔°F round-trip unchanged.
    const idSel = (suffix: string) => `[id="thermo-${TC_ID}-${suffix}"]`;
    const card = page
      .locator(idSel("name"))
      .locator("xpath=ancestor::*[contains(@class,'card')][1]");

    for (const [key, value] of Object.entries(ECO_VALUES)) {
      await page.locator(idSel(key)).fill(value);
    }

    const putResponse = page.waitForResponse(
      (r) =>
        r.url().includes(`/api/thermostats/${TC_ID}`) && r.request().method() === "PUT",
    );
    await card.getByRole("button", { name: "Save changes" }).click();
    const response = await putResponse;
    const responseText = await response.text();
    expect(
      response.status(),
      `PUT /api/thermostats/${TC_ID} failed: ${responseText}`,
    ).toBeLessThan(400);

    await page.reload();
    await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
    await page.waitForLoadState("networkidle");

    for (const [key, value] of Object.entries(ECO_VALUES)) {
      const readBack = parseFloat(await page.locator(idSel(key)).inputValue());
      expect(readBack, `eco field ${key}`).toBeCloseTo(parseFloat(value), 1);
    }
  });

  test("room Eco Mode override fields persist exactly as entered (#404)", async ({
    page,
  }) => {
    // @covers: eco_cooling_outdoor_threshold, eco_cooling_full_drift_temp, eco_cooling_max_drift, eco_heating_outdoor_threshold, eco_heating_full_drift_temp, eco_heating_max_drift, eco_hysteresis_band
    await page.goto("/rooms");
    await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
    await page.waitForLoadState("networkidle");

    const openSettings = async () => {
      await page
        .locator(".card")
        .filter({ hasText: "Living Room" })
        .first()
        .getByRole("button", { name: "Settings", exact: true })
        .click();
      const s = page.getByTestId("room-settings");
      await s.waitFor({ state: "visible", timeout: 10_000 });
      return s;
    };

    // The per-room Eco fields are nullable overrides (blank = inherit). Filling
    // a value overrides just that field; they need no outside sensor to edit.
    let settings = await openSettings();
    for (const [key, value] of Object.entries(ECO_VALUES)) {
      await settings.locator(`[id="room-${key}"]`).fill(value);
    }
    await settings.getByRole("button", { name: /Save changes/i }).click();
    await settings.waitFor({ state: "detached", timeout: 5_000 });

    settings = await openSettings();
    for (const [key, value] of Object.entries(ECO_VALUES)) {
      const readBack = parseFloat(await settings.locator(`[id="room-${key}"]`).inputValue());
      expect(readBack, `room eco field ${key}`).toBeCloseTo(parseFloat(value), 1);
    }
  });

  test("schedule target temp and drift band persist exactly as entered (#231)", async ({
    page,
  }) => {
    // @covers: target_temp, deadband_override
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

    // global-setup seeds two Mon-Fri blocks on Living Room: 08:00-17:00 and
    // 20:00-22:00 (#456). Pick the 18:00-20:00 gap between them so the overlap
    // check (a strict [start,end)) doesn't reject the save — an overlap would
    // leave the modal open and time us out.
    await modal.getByLabel(/Start time/i).fill("18:00");
    await modal.getByLabel(/End time/i).fill("20:00");
    await modal.getByLabel(/Target temperature/i).fill(SCHEDULE_TARGET);

    // The drift band exercises a DIFFERENT conversion than the target above:
    // _delta_to_f (x9/5) rather than _to_f (x9/5 + 32). A regression routing
    // it through the absolute helper would store 33.8°F for "1°C" and this
    // assertion would catch it.
    await modal.getByRole("radio", { name: /extra drift/i }).click();
    await modal.getByLabel(/^Deadband/i).fill(SCHEDULE_DRIFT);

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

    // The row badges a block that carries a band, rendered via toDisplayDelta
    // — so it re-derives the display value from °F storage just like the
    // target does.
    // Case-insensitive: the badge class uppercases its text via CSS, so the
    // rendered innerText is "±1.8°F DRIFT".
    const drift = text.match(/±\s*(\d+(?:\.\d+)?)\s*°[CF]\s*drift/i);
    expect(drift, `drift badge not found in row: ${text}`).not.toBeNull();
    expect(parseFloat(drift![1])).toBeCloseTo(parseFloat(SCHEDULE_DRIFT), 1);

    // And it survives reopening the editor: custom mode still selected, same
    // number in the input — the read path the user actually edits through.
    await newBlockRow.getByRole("button", { name: "Edit" }).click();
    const reopened = page.locator(".modal");
    await reopened.waitFor({ state: "visible", timeout: 10_000 });
    await expect(
      reopened.getByRole("radio", { name: /extra drift/i })
    ).toBeChecked();
    const band = await reopened.getByLabel(/^Deadband/i).inputValue();
    expect(parseFloat(band)).toBeCloseTo(parseFloat(SCHEDULE_DRIFT), 1);
  });
});
