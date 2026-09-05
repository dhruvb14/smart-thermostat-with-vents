import { describe, it, expect } from "vitest";
import {
  fmtTemperature,
  fmtSecondsAsHm,
  fmtMinutes,
  fmtPercent,
  localizeBinLabel,
  degreeMinutesSeries,
  fmtOvershootDelta,
} from "./format";
import { buildUnitContext } from "../../contexts";

const C = buildUnitContext("C");
const F = buildUnitContext("F");

describe("fmtTemperature — Celsius branch", () => {
  it("applies the ABSOLUTE conversion (−32 then ×5/9) for °C", () => {
    // 72 °F is a real temperature, not a delta: (72−32)×5/9 = 22.22 → "22.2°C".
    expect(fmtTemperature(72, "C")).toBe("22.2°C");
    // Freezing point pins the −32 offset: getting 0.0 wrong would mean the
    // delta formula leaked into the absolute one.
    expect(fmtTemperature(32, "C")).toBe("0.0°C");
    expect(fmtTemperature(-40, "C")).toBe("-40.0°C");
  });

  it("still short-circuits null/undefined before converting", () => {
    expect(fmtTemperature(null, "C")).toBe("—");
    expect(fmtTemperature(undefined, "C")).toBe("—");
  });

  it("defaults to °F when no unit is passed", () => {
    expect(fmtTemperature(72)).toBe("72.0°F");
    expect(fmtTemperature(72, "F")).toBe("72.0°F");
  });
});

describe("localizeBinLabel — unrecognised label falls through unchanged", () => {
  it("returns the raw label in Celsius mode when neither ≥ nor range matches", () => {
    // The backend can emit a non-numeric bin (e.g. an "overshot at all?"
    // bucket). There are no °F boundaries to convert, so it must be passed
    // through verbatim rather than mangled or dropped.
    expect(localizeBinLabel("No overshoot", C.toDisplayDelta, C.unitLabel, true)).toBe(
      "No overshoot"
    );
    expect(localizeBinLabel("", C.toDisplayDelta, C.unitLabel, true)).toBe("");
  });

  it("still converts the ≥ and range forms in Celsius mode", () => {
    expect(localizeBinLabel("≥5°F", C.toDisplayDelta, C.unitLabel, true)).toBe("≥2.8°C");
    expect(localizeBinLabel("2–3°F", C.toDisplayDelta, C.unitLabel, true)).toBe("1.1–1.7°C");
  });

  it("passes an unrecognised label through in Fahrenheit mode too", () => {
    expect(localizeBinLabel("No overshoot", F.toDisplayDelta, F.unitLabel, false)).toBe(
      "No overshoot"
    );
  });
});

describe("format edge inputs", () => {
  it("fmtSecondsAsHm handles undefined, zero and sub-minute values", () => {
    expect(fmtSecondsAsHm(undefined)).toBe("—");
    expect(fmtSecondsAsHm(0)).toBe("0m");
    expect(fmtSecondsAsHm(29)).toBe("0m");
    expect(fmtSecondsAsHm(7260)).toBe("2h 1m");
  });

  it("fmtMinutes handles undefined and zero", () => {
    expect(fmtMinutes(undefined)).toBe("—");
    expect(fmtMinutes(0)).toBe("0m");
  });

  it("fmtPercent formats zero rather than treating it as missing", () => {
    expect(fmtPercent(0)).toBe("0.0%");
    expect(fmtPercent(undefined)).toBe("—");
  });

  it("degreeMinutesSeries tolerates an undefined series and null points", () => {
    expect(degreeMinutesSeries(undefined, C.toDisplayDelta)).toEqual([]);
    const out = degreeMinutesSeries([{ period: "2024-01-02", value: null }], C.toDisplayDelta);
    expect(out).toEqual([{ period: "01-02", value: 0 }]);
  });

  it("fmtOvershootDelta short-circuits undefined", () => {
    expect(fmtOvershootDelta(undefined, C.toDisplayDelta, C.unitLabel)).toBe("—");
  });
});

// The null short-circuit above is the only part of fmtOvershootDelta that was
// pinned, which left the conversion itself — the entire reason the function
// exists — free to regress. Overshoot is a DELTA (#291/#292): a 2 °F overshoot
// is 1.1 °C, and running it through the absolute formula would print −16.7 °C,
// a negative overshoot. These assert the ×5/9-with-no-offset shape directly.
describe("fmtOvershootDelta — delta conversion (#291/#292)", () => {
  it("scales by 5/9 WITHOUT the 32° offset in Celsius mode", () => {
    expect(fmtOvershootDelta(2, C.toDisplayDelta, C.unitLabel)).toBe("1.1°C");
    expect(fmtOvershootDelta(5, C.toDisplayDelta, C.unitLabel)).toBe("2.8°C");
    // The absolute formula would give "−16.7°C" here; 0 in must stay 0 out,
    // which is the single cleanest discriminator between the two conversions.
    expect(fmtOvershootDelta(0, C.toDisplayDelta, C.unitLabel)).toBe("0.0°C");
  });

  it("is proportional — doubling the overshoot doubles the number", () => {
    // True only for the delta conversion; the absolute one is affine, so this
    // fails outright if the −32 ever creeps back in: it would give −12.8 and
    // −7.8, and −7.8 is not twice −12.8.
    //
    // 9 and 18 are chosen because ×5/9 lands on exactly 5.0 and 10.0, so the
    // 1-dp rounding in fmtOvershootDelta cannot perturb the ratio. (3 and 6
    // would round to 1.7 and 3.3, and 1.7 × 2 = 3.4 ≠ 3.3 — a rounding
    // artifact, not a conversion error.)
    const one = parseFloat(fmtOvershootDelta(9, C.toDisplayDelta, C.unitLabel));
    const two = parseFloat(fmtOvershootDelta(18, C.toDisplayDelta, C.unitLabel));
    expect(one).toBeCloseTo(5.0, 2);
    expect(two).toBeCloseTo(10.0, 2);
    expect(two).toBeCloseTo(one * 2, 2);
  });

  it("passes the magnitude through untouched in Fahrenheit mode", () => {
    expect(fmtOvershootDelta(2, F.toDisplayDelta, F.unitLabel)).toBe("2.0°F");
    expect(fmtOvershootDelta(0, F.toDisplayDelta, F.unitLabel)).toBe("0.0°F");
  });

  it("short-circuits null before touching the converter", () => {
    expect(fmtOvershootDelta(null, C.toDisplayDelta, C.unitLabel)).toBe("—");
  });
});
