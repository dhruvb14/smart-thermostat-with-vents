/**
 * Eco Mode worked-example math (Issue #404).
 *
 * A display-unit mirror of the backend's proportional-ramp relaxation
 * (`backend/eco.py`), used only to render the live "here's what these settings
 * do" examples on the Thermostats and Rooms config pages. It is unit-agnostic:
 * feed it values in whatever unit the form currently holds and the result comes
 * back in that same unit. It is NOT used to drive any HVAC decision — the engine
 *
 * KEEP IN SYNC: `backend/eco.py` owns the authoritative math; there is no
 * parity test tying the two (#420). If the relaxation formula changes there,
 * change it here in the same PR.
 * owns the real relaxation, in °F.
 */

export interface EcoExampleParams {
  coolingThreshold: number;
  coolingFullDrift: number;
  coolingMaxDrift: number;
  heatingThreshold: number;
  heatingFullDrift: number;
  heatingMaxDrift: number;
}

export type EcoNumericKey =
  | "eco_cooling_outdoor_threshold"
  | "eco_cooling_full_drift_temp"
  | "eco_cooling_max_drift"
  | "eco_heating_outdoor_threshold"
  | "eco_heating_full_drift_temp"
  | "eco_heating_max_drift"
  | "eco_hysteresis_band";

export interface EcoFieldMeta {
  key: EcoNumericKey;
  label: string;
  help: string;
  step: string;
  kind: "absolute_temp" | "delta_temp";
  group: "cooling" | "heating" | "shared";
}

// Ordered field catalog for the config pages. Absolute fields are outdoor
// temperatures; delta fields are °F/°C differences (no 32° offset).
export const ECO_NUMERIC_FIELDS: EcoFieldMeta[] = [
  {
    key: "eco_cooling_outdoor_threshold",
    label: "Cooling — outdoor threshold",
    help: "Only relax the cooling target once it is hotter than this outside.",
    step: "0.5",
    kind: "absolute_temp",
    group: "cooling",
  },
  {
    key: "eco_cooling_full_drift_temp",
    label: "Cooling — full-drift outdoor temp",
    help: "Outdoor temperature at which the full max drift is applied. Set it equal to the threshold for a hard step instead of a ramp.",
    step: "0.5",
    kind: "absolute_temp",
    group: "cooling",
  },
  {
    key: "eco_cooling_max_drift",
    label: "Cooling — max drift",
    help: "How far the cooling target may relax upward (warmer) at full drift.",
    step: "0.5",
    kind: "delta_temp",
    group: "cooling",
  },
  {
    key: "eco_heating_outdoor_threshold",
    label: "Heating — outdoor threshold",
    help: "Only relax the heating target once it is colder than this outside.",
    step: "0.5",
    kind: "absolute_temp",
    group: "heating",
  },
  {
    key: "eco_heating_full_drift_temp",
    label: "Heating — full-drift outdoor temp",
    help: "Outdoor temperature at which the full max drift is applied. Set it equal to the threshold for a hard step instead of a ramp.",
    step: "0.5",
    kind: "absolute_temp",
    group: "heating",
  },
  {
    key: "eco_heating_max_drift",
    label: "Heating — max drift",
    help: "How far the heating target may relax downward (cooler) at full drift.",
    step: "0.5",
    kind: "delta_temp",
    group: "heating",
  },
  {
    key: "eco_hysteresis_band",
    label: "Hysteresis band",
    help: "Once relaxing starts at the threshold, keep relaxing until outside moves this far back past it — prevents flapping right at the threshold.",
    step: "0.5",
    kind: "delta_temp",
    group: "shared",
  },
];

function rampFraction(distancePast: number, span: number): number {
  // span <= 0 is the degenerate/step case (full-drift at the threshold): an
  // engaged crossing jumps straight to the full drift.
  if (span <= 0) return 1;
  if (distancePast <= 0) return 0;
  if (distancePast >= span) return 1;
  return distancePast / span;
}

/**
 * The eco-relaxed target for a requested target at a given outside temperature.
 * Cooling relaxes the target warmer, heating relaxes it cooler.
 *
 * Like the backend, the relaxed target keeps its fraction (kept at 2dp) — it is
 * the temperature the room actually runs to, i.e. the cycle's stop condition.
 * Whole-degree rounding applies only to the setpoint the engine COMMANDS to
 * the thermostat device, which this preview does not model.
 */
export function ecoRelaxedTarget(
  requested: number,
  mode: "cooling" | "heating",
  outside: number,
  p: EcoExampleParams
): number {
  if (mode === "cooling") {
    if (outside < p.coolingThreshold) return requested;
    const f = rampFraction(outside - p.coolingThreshold, p.coolingFullDrift - p.coolingThreshold);
    return Math.round((requested + f * p.coolingMaxDrift) * 100) / 100;
  }
  if (outside > p.heatingThreshold) return requested;
  const f = rampFraction(p.heatingThreshold - outside, p.heatingThreshold - p.heatingFullDrift);
  return Math.round((requested - f * p.heatingMaxDrift) * 100) / 100;
}
