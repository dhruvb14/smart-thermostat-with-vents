import { describe, it, expect } from "vitest";
import { renderHook } from "@testing-library/react";
import { buildUnitContext, useUnit } from "./contexts";

describe("buildUnitContext — F mode", () => {
  const ctx = buildUnitContext("F");

  it("has correct unit metadata", () => {
    expect(ctx.unit).toBe("F");
    expect(ctx.isCelsius).toBe(false);
    expect(ctx.unitLabel).toBe("°F");
  });

  it("toDisplay is identity", () => {
    expect(ctx.toDisplay(70)).toBe(70);
    expect(ctx.toDisplay(32)).toBe(32);
  });

  it("toDisplayDelta is identity", () => {
    expect(ctx.toDisplayDelta(0.5)).toBe(0.5);
    expect(ctx.toDisplayDelta(3)).toBe(3);
  });

  it("toStorage is identity", () => {
    expect(ctx.toStorage(70)).toBe(70);
  });

  it("toStorageDelta is identity", () => {
    expect(ctx.toStorageDelta(0.5)).toBe(0.5);
    expect(ctx.toStorageDelta(3)).toBe(3);
  });

  it("fmtTemp formats with °F", () => {
    expect(ctx.fmtTemp(70)).toBe("70.0°F");
  });
});

describe("buildUnitContext — C mode", () => {
  const ctx = buildUnitContext("C");

  it("has correct unit metadata", () => {
    expect(ctx.unit).toBe("C");
    expect(ctx.isCelsius).toBe(true);
    expect(ctx.unitLabel).toBe("°C");
  });

  it("toDisplay converts °F to °C", () => {
    expect(ctx.toDisplay(32)).toBe(0.0);
    expect(ctx.toDisplay(212)).toBe(100.0);
    expect(ctx.toDisplay(69.8)).toBe(21.0);
  });

  it("toDisplayDelta converts °F delta to °C delta", () => {
    expect(ctx.toDisplayDelta(0)).toBe(0);
    expect(ctx.toDisplayDelta(9)).toBe(5);
    expect(ctx.toDisplayDelta(1)).toBe(0.56);
  });

  it("toStorage converts °C to °F", () => {
    expect(ctx.toStorage(0)).toBe(32.0);
    expect(ctx.toStorage(100)).toBe(212.0);
    expect(ctx.toStorage(21)).toBe(69.8);
  });

  it("toStorageDelta converts °C delta to °F delta", () => {
    expect(ctx.toStorageDelta(0)).toBe(0);
    expect(ctx.toStorageDelta(5)).toBe(9);
    expect(ctx.toStorageDelta(1)).toBe(1.8);
  });

  it("fmtTemp formats with °C", () => {
    expect(ctx.fmtTemp(212)).toBe("100.0°C");
    expect(ctx.fmtTemp(69.8)).toBe("21.0°C");
  });
});

describe("useUnit default context", () => {
  it("returns F unit defaults from buildUnitContext", () => {
    const { result } = renderHook(() => useUnit());
    expect(result.current.unit).toBe("F");
    expect(result.current.isCelsius).toBe(false);
    expect(result.current.unitLabel).toBe("°F");
    expect(result.current.toDisplay(70)).toBe(70);
    expect(result.current.toStorage(70)).toBe(70);
    expect(result.current.fmtTemp(70)).toBe("70.0°F");
  });
});

describe("displayBound (#521)", () => {
  const c = buildUnitContext("C");
  const f = buildUnitContext("F");

  it("is identity in Fahrenheit", () => {
    expect(f.displayBound(40, "min")).toBe(40);
    expect(f.displayBound(90, "max")).toBe(90);
    expect(f.displayBound(0, "min", "delta")).toBe(0);
    expect(f.displayBound(10, "max", "delta")).toBe(10);
  });

  it("nudges a °C bound inward when the naive conversion falls outside", () => {
    // toDisplay(40) is 4.4, which converts back to 39.92 — below the bound.
    expect(c.toDisplay(40)).toBe(4.4);
    expect(c.toStorage(4.4)).toBeLessThan(40);
    expect(c.displayBound(40, "min")).toBe(4.5);

    // toDisplayDelta(10) is 5.56 → 10.01, above the bound.
    expect(c.toDisplayDelta(10)).toBe(5.56);
    expect(c.toStorageDelta(5.56)).toBeGreaterThan(10);
    expect(c.displayBound(10, "max", "delta")).toBe(5.55);
  });

  it("leaves a bound alone when the naive conversion already fits", () => {
    expect(c.toStorage(c.toDisplay(90))).toBeLessThanOrEqual(90);
    expect(c.displayBound(90, "max")).toBe(c.toDisplay(90));
    expect(c.displayBound(0, "min", "delta")).toBe(0);
  });

  it("always yields a value the backend's range check accepts", () => {
    // The property that matters: whatever it returns must survive the round
    // trip back into range, for every bound the forms use.
    const cases: [number, "min" | "max", "absolute" | "delta"][] = [
      [40, "min", "absolute"],
      [90, "max", "absolute"],
      [0, "min", "delta"],
      [10, "max", "delta"],
    ];
    for (const [fahrenheit, side, kind] of cases) {
      for (const ctx of [c, f]) {
        const bound = ctx.displayBound(fahrenheit, side, kind);
        const back = kind === "delta" ? ctx.toStorageDelta(bound) : ctx.toStorage(bound);
        if (side === "min") expect(back).toBeGreaterThanOrEqual(fahrenheit);
        else expect(back).toBeLessThanOrEqual(fahrenheit);
      }
    }
  });
});
