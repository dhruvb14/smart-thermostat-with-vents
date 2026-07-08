/**
 * The shared Eco Mode live dual cooling/heating worked example (Issue #404),
 * used by both the Thermostats and Rooms config pages. The field catalog and
 * relaxation math live in ``../eco``; this file holds only the component so
 * fast-refresh stays happy.
 */

import { useUnit } from "../contexts";
import { ecoRelaxedTarget, type EcoExampleParams } from "../eco";

/**
 * The live dual worked example. ``params`` are the effective values in DISPLAY
 * units (already resolved for inheritance on the room page). A sample indoor
 * target of 70 °F / 21 °C matches the driving example in the docs.
 */
export function EcoWorkedExample({ params }: { params: EcoExampleParams }) {
  const { unitLabel, isCelsius } = useUnit();
  const indoor = isCelsius ? 21 : 70;
  const fmt = (v: number) => `${Math.round(v * 10) / 10}${unitLabel}`;
  const coolRelaxed = ecoRelaxedTarget(indoor, "cooling", params.coolingFullDrift, params);
  const heatRelaxed = ecoRelaxedTarget(indoor, "heating", params.heatingFullDrift, params);

  // Fixed illustration of the target-vs-setpoint split in the active display
  // unit: a fractional mid-ramp relaxed target the room runs to as-is.
  const fractionalTarget = isCelsius ? 21.6 : 71.6;

  return (
    <div className="form-hint" data-testid="eco-worked-example">
      <strong>Cooling example:</strong> a {fmt(indoor)} room holds {fmt(indoor)} until it is{" "}
      {fmt(params.coolingThreshold)} outside, then relaxes up toward {fmt(coolRelaxed)} once it hits{" "}
      {fmt(params.coolingFullDrift)} outside.
      <br />
      <strong>Heating example:</strong> a {fmt(indoor)} room holds {fmt(indoor)} until it is{" "}
      {fmt(params.heatingThreshold)} outside, then relaxes down toward {fmt(heatRelaxed)} once it
      hits {fmt(params.heatingFullDrift)} outside.
      <br />
      <strong>Fractions:</strong> mid-ramp the relaxed target can be a fraction, e.g. a computed{" "}
      {fmt(fractionalTarget)} — the room runs until it actually reaches that value. Only the
      setpoint sent to the thermostat is rounded to the closest whole degree (.5 rounds up), since
      most thermostats do not support partial temperatures.
    </div>
  );
}
