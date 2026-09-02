/**
 * Shared Eco Mode defaults for test fixtures (Issue #404).
 *
 * The ThermostatConfig / Room mock literals across the page tests must stay
 * complete as the types grow. Spreading these keeps the Eco fields in one place
 * so a future Eco field only needs adding here, not in every fixture.
 */
import type { RoomOverrideHold } from "./api";

export const ecoThermostatDefaults = {
  eco_mode_enabled: false,
  eco_cooling_outdoor_threshold: 86,
  eco_cooling_full_drift_temp: 100,
  eco_cooling_max_drift: 4,
  eco_heating_outdoor_threshold: 40,
  eco_heating_full_drift_temp: 0,
  eco_heating_max_drift: 4,
  eco_hysteresis_band: 2,
  // Eco Suspend (#500): read-only per-thermostat suspension state.
  eco_suspend_until: null as string | null,
};

export const ecoRoomDefaults = {
  eco_mode_enabled: null,
  eco_cooling_outdoor_threshold: null,
  eco_cooling_full_drift_temp: null,
  eco_cooling_max_drift: null,
  eco_heating_outdoor_threshold: null,
  eco_heating_full_drift_temp: null,
  eco_heating_max_drift: null,
  eco_hysteresis_band: null,
};

/**
 * Live temporary-hold row as returned by GET /api/overrides (Issue #576).
 *
 * Shared by the HoldModal / Dashboard / Rooms / Schedules suites so the hold
 * shape stays complete in one place as the type grows. target_temp is raw °F
 * (75 → "75.0°F" / "23.9°C"); ends_in_seconds 5400 renders as "1h 30m".
 */
export const makeHold = (over: Partial<RoomOverrideHold> = {}): RoomOverrideHold => ({
  room_id: "room-1",
  target_temp: 75,
  expires_at: "2099-01-01T12:00:00",
  respect_eco: false,
  ends_in_seconds: 5400,
  ...over,
});
