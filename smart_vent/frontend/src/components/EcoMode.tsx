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

  // Fixed illustrations for the rounding rule — one that rounds down and one
  // that rounds up — in the active display unit.
  const roundDownRaw = isCelsius ? 21.4 : 71.4;
  const roundUpRaw = isCelsius ? 21.6 : 71.6;

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
      <strong>Rounding:</strong> most thermostats do not support partial temperatures, so the
      relaxed target is rounded to the closest whole number (.5 rounds up). Mid-ramp, a computed{" "}
      {fmt(roundDownRaw)} runs as {fmt(Math.round(roundDownRaw))} (rounded down) and a computed{" "}
      {fmt(roundUpRaw)} runs as {fmt(Math.round(roundUpRaw))} (rounded up). The dashboard keeps the
      🌿 Eco badge even when rounding lands back on the requested temperature.
    </div>
  );
}
