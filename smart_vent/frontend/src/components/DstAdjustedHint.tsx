import { useTimeZone } from "../contexts";

/**
 * Shown under a date/time input when the typed time does not exist in the
 * house's zone — the hour skipped by a spring-forward DST change. The time is
 * saved moved forward by the gap (02:30 → 03:30), and this says so out loud
 * instead of silently storing something else (Issue #729).
 */
export default function DstAdjustedHint({ value }: { value: string }) {
  const tz = useTimeZone();
  const parsed = value ? tz.fromInput(value) : null;
  if (!parsed?.adjusted) return null;
  const saved = tz.format(parsed.iso);
  return (
    <div className="form-hint" role="status">
      {`That time is skipped by a daylight-saving change, so it will be saved as ${saved}.`}
    </div>
  );
}
