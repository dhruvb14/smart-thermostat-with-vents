/**
 * Compact remaining-time string for status rows and hold cards.
 *
 * Lifted from Rooms.tsx when the temporary-hold UI (#576) needed the same
 * rendering on the Dashboard and Schedules pages. Every rendered call site
 * must wrap the result in <Frozen> — countdowns are time-varying UI and the
 * visual-regression goldens never stabilise otherwise (CLAUDE.md pitfall 8).
 */
export function formatCountdown(totalSeconds: number): string {
  if (totalSeconds <= 0) return "ending…";
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}
