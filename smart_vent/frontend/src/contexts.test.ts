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

  it("toStorage is identity", () => {
    expect(ctx.toStorage(70)).toBe(70);
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

  it("toStorage converts °C to °F", () => {
    expect(ctx.toStorage(0)).toBe(32.0);
    expect(ctx.toStorage(100)).toBe(212.0);
    expect(ctx.toStorage(21)).toBe(69.8);
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
