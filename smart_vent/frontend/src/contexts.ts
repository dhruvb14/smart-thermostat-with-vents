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
// Unit context
// ---------------------------------------------------------------------------

export interface UnitContextValue {
  unit: "F" | "C";
  isCelsius: boolean;
  /** Convert a stored °F value to the active display unit. */
  toDisplay: (fahrenheit: number) => number;
  /** Convert an active-unit value back to °F (for local comparisons). */
  toStorage: (displayValue: number) => number;
  /** Format a stored °F value with the active unit label (1dp). */
  fmtTemp: (fahrenheit: number) => string;
  unitLabel: "°F" | "°C";
}

export const UnitContext = createContext<UnitContextValue>({
  unit: "F",
  isCelsius: false,
  toDisplay: (f) => f,
  toStorage: (v) => v,
  fmtTemp: (f) => `${f.toFixed(1)}°F`,
  unitLabel: "°F",
});

export function useUnit() {
  return useContext(UnitContext);
}
