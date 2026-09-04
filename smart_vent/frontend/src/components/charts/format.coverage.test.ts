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
