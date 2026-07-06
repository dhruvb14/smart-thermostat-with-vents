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
