/**
 * Shared Eco Mode defaults for test fixtures (Issue #404).
 *
 * The ThermostatConfig / Room mock literals across the page tests must stay
 * complete as the types grow. Spreading these keeps the Eco fields in one place
 * so a future Eco field only needs adding here, not in every fixture.
 */

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
