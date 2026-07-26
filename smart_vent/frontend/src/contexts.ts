import { createContext, useContext } from "react";

// ---------------------------------------------------------------------------
// System context
// ---------------------------------------------------------------------------

export interface SystemContextValue {
  enabled: boolean;
  toggle: () => Promise<void>;
}

export const SystemContext = createContext<SystemContextValue>({
  enabled: true,
  toggle: async () => {},
});

export function useSystem() {
  return useContext(SystemContext);
}

// ---------------------------------------------------------------------------
// Developer mode context
// ---------------------------------------------------------------------------

export interface DevModeContextValue {
  devMode: boolean;
  toggleDevMode: () => Promise<void>;
}

export const DevModeContext = createContext<DevModeContextValue>({
  devMode: false,
  toggleDevMode: async () => {},
});

export function useDevMode() {
  return useContext(DevModeContext);
}

// ---------------------------------------------------------------------------
// MCP server context (HTTP MCP toggle in the settings cog)
// ---------------------------------------------------------------------------

export interface McpContextValue {
  mcpEnabled: boolean;
  toggleMcp: () => Promise<void>;
}

export const McpContext = createContext<McpContextValue>({
  mcpEnabled: false,
  toggleMcp: async () => {},
});

export function useMcp() {
  return useContext(McpContext);
}

// ---------------------------------------------------------------------------
// Auth context (#373 — direct-port session; the login gate lives in App.tsx)
// ---------------------------------------------------------------------------

export type AuthMethod = "open" | "ingress" | "session" | "none";

export interface AuthContextValue {
  // Whether the direct-port/MCP auth boundary is enforced (add-on option).
  requireAuth: boolean;
  // How the current caller is authenticated.
  method: AuthMethod;
  // Clear the direct-port session (no-op for ingress/open callers).
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue>({
  requireAuth: false,
  method: "open",
  logout: async () => {},
});

export function useAuth() {
  return useContext(AuthContext);
}

// ---------------------------------------------------------------------------
// Theme context (light / dark / system — settings-cog control)
// ---------------------------------------------------------------------------

export type Theme = "light" | "dark" | "system";

export interface ThemeContextValue {
  theme: Theme;
  /** Persist the next theme and apply it. */
  setTheme: (theme: Theme) => Promise<void>;
}

export const ThemeContext = createContext<ThemeContextValue>({
  theme: "system",
  setTheme: async () => {},
});

export function useTheme() {
  return useContext(ThemeContext);
}

/** Reflect a theme choice onto <html>: an explicit choice pins data-theme so
 * the CSS overrides win; "system" removes it so prefers-color-scheme rules. */
export function applyThemeToDocument(theme: Theme) {
  if (theme === "system") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", theme);
  }
}

// ---------------------------------------------------------------------------
// Unit context
// ---------------------------------------------------------------------------

export interface UnitContextValue {
  unit: "F" | "C";
  isCelsius: boolean;
  /** Convert a stored °F value to the active display unit. */
  toDisplay: (fahrenheit: number) => number;
  /** Convert a stored °F delta (e.g. deadband, offset) to the active display unit. */
  toDisplayDelta: (fahrenheitDelta: number) => number;
  /** Convert an active-unit value back to °F for storage. */
  toStorage: (displayValue: number) => number;
  /** Convert an active-unit delta back to °F delta for storage. */
  toStorageDelta: (displayDelta: number) => number;
  /** Format a stored °F value with the active unit label (1dp). */
  fmtTemp: (fahrenheit: number) => string;
  /**
   * A °F validation bound expressed as a display-unit value the backend will
   * actually accept.
   *
   * Converting a °F limit for display ROUNDS, and rounding can move the bound
   * outward: `toDisplay(40)` is 4.4 °C, which converts back to 39.92 °F and
   * fails a `40 <= v` check. A form comparing against the raw converted bound
   * therefore advertises — and accepts — a value the backend refuses, and says
   * so in an error naming the very number the user typed. `toDisplayDelta(10)`
   * is 5.56 °C → 10.01 °F, the same failure on the delta side.
   *
   * This nudges the bound inward by the display helper's own precision until
   * the round trip lands inside the range, so `min`/`max` attributes, error
   * messages, and comparisons all use a value that can be saved. Identity in
   * Fahrenheit, where every conversion is exact.
   */
  displayBound: (fahrenheit: number, side: "min" | "max", kind?: "absolute" | "delta") => number;
  unitLabel: "°F" | "°C";
}

/** Build a UnitContextValue for the given unit. Used by AppRoot and in tests. */
export function buildUnitContext(unit: "F" | "C"): UnitContextValue {
  const isCelsius = unit === "C";
  const toDisplay = isCelsius
    ? (f: number) => parseFloat(((f - 32) * (5 / 9)).toFixed(1))
    : (f: number) => f;
  const toDisplayDelta = isCelsius
    ? (f: number) => parseFloat((f * (5 / 9)).toFixed(2))
    : (f: number) => f;
  const toStorage = isCelsius
    ? (c: number) => parseFloat((c * (9 / 5) + 32).toFixed(2))
    : (f: number) => f;
  const toStorageDelta = isCelsius
    ? (c: number) => parseFloat((c * (9 / 5)).toFixed(2))
    : (f: number) => f;
  const fmtTemp = (f: number) => `${toDisplay(f).toFixed(1)}${isCelsius ? "°C" : "°F"}`;
  const displayBound = (
    fahrenheit: number,
    side: "min" | "max",
    kind: "absolute" | "delta" = "absolute"
  ): number => {
    const isDelta = kind === "delta";
    const toD = isDelta ? toDisplayDelta : toDisplay;
    const toS = isDelta ? toStorageDelta : toStorage;
    // Match the precision the display helper rounds to, so a step lands on a
    // value the helper could itself have produced.
    const decimals = isDelta ? 2 : 1;
    const step = 10 ** -decimals;
    let value = toD(fahrenheit);
    // At most a few steps: the round-trip error is bounded by one ulp of the
    // display precision, so this converges immediately or not at all.
    for (let i = 0; i < 4; i++) {
      const roundTripped = toS(value);
      if (side === "min" ? roundTripped >= fahrenheit : roundTripped <= fahrenheit) return value;
      value = parseFloat((value + (side === "min" ? step : -step)).toFixed(decimals));
    }
    return value;
  };
  return {
    unit,
    isCelsius,
    toDisplay,
    toDisplayDelta,
    toStorage,
    toStorageDelta,
    fmtTemp,
    displayBound,
    unitLabel: isCelsius ? "°C" : "°F",
  };
}

export const UnitContext = createContext<UnitContextValue>(buildUnitContext("F"));

export function useUnit() {
  return useContext(UnitContext);
}
