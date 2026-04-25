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
