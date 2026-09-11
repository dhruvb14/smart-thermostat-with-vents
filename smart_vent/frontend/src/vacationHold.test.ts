import { describe, expect, it } from "vitest";

import { buildUnitContext } from "./contexts";
import { vacationHoldTarget } from "./vacationHold";

describe("vacationHoldTarget (#628)", () => {
  it("insets the heating target by one deadband above the floor", () => {
    expect(vacationHoldTarget(62, 2, 80, "heat")).toBe(64);
  });

  it("insets the cooling target by one deadband below the ceiling", () => {
    expect(vacationHoldTarget(80, 2, 62, "cool")).toBe(78);
  });

  it("clamps to the opposite bound when the deadband is wider than the band", () => {
    // Band 70–74 with a 5°F deadband: the raw insets (75 / 69) would each sit
    // past the opposite bound and hand the next tick a breach the other way.
    expect(vacationHoldTarget(70, 5, 74, "heat")).toBe(74);
    expect(vacationHoldTarget(74, 5, 70, "cool")).toBe(70);
  });

  it("returns null when any input is missing, so the hint renders nothing", () => {
    expect(vacationHoldTarget(null, 2, 80, "heat")).toBeNull();
    expect(vacationHoldTarget(62, undefined, 80, "heat")).toBeNull();
    expect(vacationHoldTarget(62, 2, null, "heat")).toBeNull();
  });

  it("rounds to one decimal rather than showing float noise", () => {
    expect(vacationHoldTarget(16.7, 1.1, 26.7, "heat")).toBe(17.8);
  });

  it("composes correctly in Celsius because the deadband is a DELTA", () => {
    // The form holds display units: an absolute bound via toDisplay, the
    // deadband via toDisplayDelta. Adding them must equal converting the °F
    // target, which only works because the delta conversion omits the −32.
    const { toDisplay, toDisplayDelta } = buildUnitContext("C");
    const minF = 62;
    const deadbandF = 2;

    const fromDisplayInputs = vacationHoldTarget(
      toDisplay(minF),
      toDisplayDelta(deadbandF),
      toDisplay(80),
      "heat"
    );

    expect(fromDisplayInputs).toBe(Math.round(toDisplay(minF + deadbandF) * 10) / 10);
    // And it is NOT what using the absolute helper on the deadband would give
    // — that would subtract 32 and land ~17.8°C low.
    expect(fromDisplayInputs).not.toBe(
      Math.round((toDisplay(minF) + toDisplay(deadbandF)) * 10) / 10
    );
  });
});
