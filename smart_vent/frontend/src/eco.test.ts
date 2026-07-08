import { describe, it, expect } from "vitest";
import { ecoRelaxedTarget, ECO_NUMERIC_FIELDS, type EcoExampleParams } from "./eco";

const P: EcoExampleParams = {
  coolingThreshold: 86,
  coolingFullDrift: 100,
  coolingMaxDrift: 4,
  heatingThreshold: 40,
  heatingFullDrift: 0,
  heatingMaxDrift: 4,
};

describe("ecoRelaxedTarget — cooling", () => {
  it("does not relax below the threshold", () => {
    expect(ecoRelaxedTarget(70, "cooling", 80, P)).toBe(70);
  });
  it("holds at the threshold (f=0)", () => {
    expect(ecoRelaxedTarget(70, "cooling", 86, P)).toBe(70);
  });
  it("relaxes proportionally at the midpoint", () => {
    expect(ecoRelaxedTarget(70, "cooling", 93, P)).toBe(72);
  });
  it("reaches max drift at the full-drift temp", () => {
    expect(ecoRelaxedTarget(70, "cooling", 100, P)).toBe(74);
  });
  it("clamps at max drift beyond the full-drift temp", () => {
    expect(ecoRelaxedTarget(70, "cooling", 120, P)).toBe(74);
  });
});

describe("ecoRelaxedTarget — heating", () => {
  it("does not relax above the threshold", () => {
    expect(ecoRelaxedTarget(70, "heating", 60, P)).toBe(70);
  });
  it("relaxes proportionally at the midpoint", () => {
    expect(ecoRelaxedTarget(70, "heating", 20, P)).toBe(68);
  });
  it("reaches max drift at the full-drift temp", () => {
    expect(ecoRelaxedTarget(70, "heating", 0, P)).toBe(66);
  });
  it("clamps beyond the full-drift temp", () => {
    expect(ecoRelaxedTarget(70, "heating", -20, P)).toBe(66);
  });
});

describe("ecoRelaxedTarget — fractional relaxed targets (no whole-degree rounding)", () => {
  // Mirrors backend/eco.py relax_target: the relaxed target is the room's stop
  // condition and keeps its fraction. Whole-degree rounding belongs only to
  // the setpoint the engine commands to the device, which the preview does
  // not model.
  it("keeps a mid-ramp fraction (71.43)", () => {
    // f = (91-86)/14 → +1.43
    expect(ecoRelaxedTarget(70, "cooling", 91, P)).toBeCloseTo(71.43, 2);
  });
  it("keeps a fraction above the half (72.57)", () => {
    // f = (95-86)/14 → +2.57
    expect(ecoRelaxedTarget(70, "cooling", 95, P)).toBeCloseTo(72.57, 2);
  });
  it("keeps an exact half (72.5)", () => {
    // f = (94.75-86)/14 = 0.625 → +2.5
    expect(ecoRelaxedTarget(70, "cooling", 94.75, P)).toBe(72.5);
  });
  it("heating: keeps an exact half (68.5)", () => {
    // f = (40-25)/40 = 0.375 → −1.5
    expect(ecoRelaxedTarget(70, "heating", 25, P)).toBe(68.5);
  });
  it("keeps even a tiny relaxation (70.29) instead of collapsing to the requested target", () => {
    // f = (87-86)/14 → +0.29
    expect(ecoRelaxedTarget(70, "cooling", 87, P)).toBeCloseTo(70.29, 2);
  });
  it("passes fractional requests through un-engaged", () => {
    expect(ecoRelaxedTarget(70.4, "cooling", 80, P)).toBe(70.4);
  });
});

describe("ecoRelaxedTarget — degenerate step (full-drift == threshold)", () => {
  const step: EcoExampleParams = { ...P, coolingFullDrift: 86, heatingFullDrift: 40 };
  it("jumps to full drift the instant cooling threshold is crossed", () => {
    expect(ecoRelaxedTarget(70, "cooling", 87, step)).toBe(74);
  });
  it("jumps to full drift the instant heating threshold is crossed", () => {
    expect(ecoRelaxedTarget(70, "heating", 39, step)).toBe(66);
  });
});

describe("ECO_NUMERIC_FIELDS catalog", () => {
  it("has the seven numeric fields with valid kinds", () => {
    expect(ECO_NUMERIC_FIELDS).toHaveLength(7);
    for (const f of ECO_NUMERIC_FIELDS) {
      expect(["absolute_temp", "delta_temp"]).toContain(f.kind);
      expect(f.key.startsWith("eco_")).toBe(true);
    }
  });
});
