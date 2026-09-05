import { describe, it, expect, afterEach } from "vitest";
import { renderHook } from "@testing-library/react";
import {
  applyThemeToDocument,
  buildUnitContext,
  useSystem,
  useDevMode,
  useMcp,
  useAuth,
  useTheme,
  useUnit,
} from "./contexts";

// ---------------------------------------------------------------------------
// Default context values
// ---------------------------------------------------------------------------
// Every context ships an inert default so a component rendered outside its
// provider (a test harness, a Storybook-style island, an error path that
// unmounts AppRoot) degrades to "nothing happens" instead of crashing on
// `undefined is not a function`. Nothing else in the suite ever calls those
// no-ops, so their bodies were never executed.

describe("context defaults outside a provider", () => {
  it("SystemContext reads enabled and its toggle resolves without doing anything", async () => {
    const { result } = renderHook(() => useSystem());
    expect(result.current.enabled).toBe(true);
    await expect(result.current.toggle()).resolves.toBeUndefined();
    // The no-op must not mutate the value it was read from.
    expect(result.current.enabled).toBe(true);
  });

  it("DevModeContext defaults to off and its toggle is inert", async () => {
    const { result } = renderHook(() => useDevMode());
    expect(result.current.devMode).toBe(false);
    await expect(result.current.toggleDevMode()).resolves.toBeUndefined();
    expect(result.current.devMode).toBe(false);
  });

  it("McpContext defaults to off and its toggle is inert", async () => {
    const { result } = renderHook(() => useMcp());
    expect(result.current.mcpEnabled).toBe(false);
    await expect(result.current.toggleMcp()).resolves.toBeUndefined();
    expect(result.current.mcpEnabled).toBe(false);
  });

  it("AuthContext defaults to an open, un-gated caller whose logout is inert", async () => {
    const { result } = renderHook(() => useAuth());
    expect(result.current.requireAuth).toBe(false);
    expect(result.current.method).toBe("open");
    await expect(result.current.logout()).resolves.toBeUndefined();
  });

  it("ThemeContext defaults to system and its setTheme does not touch the document", async () => {
    document.documentElement.removeAttribute("data-theme");
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("system");
    await expect(result.current.setTheme("dark")).resolves.toBeUndefined();
    // The default is a no-op, not a shortcut into applyThemeToDocument — a
    // provider-less component must not be able to repaint the whole app.
    expect(document.documentElement.getAttribute("data-theme")).toBeNull();
  });
});

describe("applyThemeToDocument", () => {
  afterEach(() => document.documentElement.removeAttribute("data-theme"));

  it("pins data-theme for an explicit choice and clears it for system", () => {
    applyThemeToDocument("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    applyThemeToDocument("light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    applyThemeToDocument("system");
    expect(document.documentElement.getAttribute("data-theme")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Absolute vs delta conversion (the #231-adjacent bug class)
// ---------------------------------------------------------------------------

describe("absolute vs delta conversion", () => {
  const c = buildUnitContext("C");
  const f = buildUnitContext("F");

  it("absolute conversions carry the 32° offset in both directions", () => {
    // (f - 32) * 5/9 out, c * 9/5 + 32 back.
    expect(c.toDisplay(32)).toBe(0);
    expect(c.toDisplay(70)).toBe(21.1);
    expect(c.toStorage(0)).toBe(32);
    expect(c.toStorage(16)).toBe(60.8); // the value from the #231 write-up
  });

  it("delta conversions apply the ratio ONLY — never the 32° offset", () => {
    // A 0°F deadband is a 0°C deadband. Running a delta through the absolute
    // helper would report -17.8, which is the corruption pitfall #4 describes.
    expect(c.toDisplayDelta(0)).toBe(0);
    expect(c.toDisplayDelta(0)).not.toBe(c.toDisplay(0));
    expect(c.toStorageDelta(0)).toBe(0);
    expect(c.toStorageDelta(0)).not.toBe(c.toStorage(0));

    // 1 °F of deadband is 5/9 °C; 0.5 °F of overshoot is 0.28 °C (2dp).
    expect(c.toDisplayDelta(1)).toBe(0.56);
    expect(c.toDisplayDelta(0.5)).toBe(0.28);
    expect(c.toStorageDelta(0.56)).toBe(1.01);
    expect(c.toStorageDelta(5)).toBe(9);
  });

  it("delta and absolute differ by exactly the offset for the same number", () => {
    for (const v of [0, 1, 9, 40, 70]) {
      expect(c.toDisplay(v)).toBeCloseTo(c.toDisplayDelta(v) - c.toDisplayDelta(32), 1);
      expect(c.toStorage(v)).toBeCloseTo(c.toStorageDelta(v) + 32, 2);
    }
  });

  it("round-trips within display precision in both units and both kinds", () => {
    for (const fahrenheit of [32, 40, 60.8, 68, 70, 72.5, 90]) {
      // °C display rounds to 1dp, so the round trip can land up to half a
      // display step (0.05 °C ≈ 0.09 °F) away — the exact slack `displayBound`
      // exists to absorb. It must never be worse than one whole step.
      expect(Math.abs(c.toStorage(c.toDisplay(fahrenheit)) - fahrenheit)).toBeLessThanOrEqual(0.1);
      expect(f.toStorage(f.toDisplay(fahrenheit))).toBe(fahrenheit);
    }
    for (const delta of [0, 0.5, 1, 2, 3, 9, 10]) {
      expect(c.toStorageDelta(c.toDisplayDelta(delta))).toBeCloseTo(delta, 1);
      expect(f.toStorageDelta(f.toDisplayDelta(delta))).toBe(delta);
    }
    for (const celsius of [0, 16, 20, 21, 25]) {
      expect(c.toDisplay(c.toStorage(celsius))).toBeCloseTo(celsius, 1);
    }
  });

  it("fmtTemp always renders 1dp with the active label", () => {
    expect(f.fmtTemp(70)).toBe("70.0°F");
    expect(f.fmtTemp(72.55)).toBe("72.5°F"); // formatting only — no conversion
    expect(c.fmtTemp(32)).toBe("0.0°C");
    expect(c.fmtTemp(70)).toBe("21.1°C");
    // fmtTemp is the ABSOLUTE formatter; a delta must never go through it.
    expect(c.fmtTemp(0)).toBe("-17.8°C");
  });

  it("useUnit's default context is the Fahrenheit build (identity everywhere)", () => {
    const { result } = renderHook(() => useUnit());
    expect(result.current.toDisplayDelta(3)).toBe(3);
    expect(result.current.toStorageDelta(3)).toBe(3);
    expect(result.current.fmtTemp(68)).toBe("68.0°F");
    expect(result.current.displayBound(40, "min")).toBe(40);
  });
});
