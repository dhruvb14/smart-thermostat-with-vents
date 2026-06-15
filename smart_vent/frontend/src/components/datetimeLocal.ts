/**
 * Format a Date as a `datetime-local` input value (`YYYY-MM-DDTHH:mm`) from its
 * LOCAL components.
 *
 * A `datetime-local` input interprets its `value`/`min`/`max` as local time, so
 * formatting via `toISOString()` (UTC) shifts the bound by the UTC offset — west
 * of UTC that rejects perfectly valid near-future times. (Issue #294)
 */
export function toDatetimeLocalString(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}
