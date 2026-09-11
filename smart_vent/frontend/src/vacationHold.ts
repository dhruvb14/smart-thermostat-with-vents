/** The vacation hold's recovery target, in DISPLAY units (Issue #628).
 *
 * The hold TRIGGERS on the bare bound but RECOVERS to one deadband inside it,
 * clamped to the opposite bound so a deadband wider than the band cannot make
 * heating overshoot into calling for cooling. Mirrors `_apply_vacation_hold`
 * in `cycle_engine.py`.
 *
 * Every argument is already a display-unit form value: an absolute bound plus
 * a DELTA deadband. That composes correctly in °C as well as °F —
 * (f − 32) × 5/9 + d × 5/9 — so no conversion belongs here, and using the
 * absolute helper on the deadband would subtract 32 and corrupt it.
 */
export function vacationHoldTarget(
  bound: number | null | undefined,
  deadband: number | null | undefined,
  oppositeBound: number | null | undefined,
  direction: "heat" | "cool"
): number | null {
  if (bound == null || deadband == null || oppositeBound == null) return null;
  const raw = direction === "heat" ? bound + deadband : bound - deadband;
  const clamped =
    direction === "heat" ? Math.min(oppositeBound, raw) : Math.max(oppositeBound, raw);
  return Math.round(clamped * 10) / 10;
}
