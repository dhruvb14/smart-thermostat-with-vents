// Tiny shared formatters for chart axes + tooltips. Keeping these here so
// every chart renders consistent labels.

export function fmtSecondsAsHm(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  if (h >= 1) return `${h}h ${m}m`;
  return `${m}m`;
}

export function fmtMinutes(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  return `${Math.round(seconds / 60)}m`;
}

export function fmtPercent(value: number | null | undefined, digits = 1): string {
  if (value == null) return "—";
  return `${value.toFixed(digits)}%`;
}

export function fmtTemperature(value: number | null | undefined, unit: "F" | "C" = "F"): string {
  if (value == null) return "—";
  if (unit === "C") return `${((value - 32) * (5 / 9)).toFixed(1)}°C`;
  return `${value.toFixed(1)}°F`;
}

/** Strip leading "YYYY-" so day labels read "MM-DD" — keeps x-axis tight. */
export function shortDayLabel(period: string): string {
  // period is "YYYY-MM-DD" from the timeseries endpoint.
  return period.length === 10 ? period.slice(5) : period;
}

// ---------------------------------------------------------------------------
// Unit-aware DELTA formatters (Issues #291, #292)
//
// Overshoot magnitudes and degree-minutes are temperature *deltas*, so they
// must use the delta conversion (×5/9, no −32). Using the absolute conversion
// (`fmtTemperature`) on a delta yields nonsense — e.g. a 2 °F overshoot shows
// as −16.7 °C.
// ---------------------------------------------------------------------------

/** Format an overshoot delta in the active unit, e.g. "1.1°C" / "2.0°F". */
export function fmtOvershootDelta(
  value: number | null | undefined,
  toDisplayDelta: (fahrenheitDelta: number) => number,
  unitLabel: string
): string {
  if (value == null) return "—";
  return `${toDisplayDelta(value).toFixed(1)}${unitLabel}`;
}

/**
 * Convert a backend overshoot-histogram bin label (°F boundaries, e.g. "0–1°F"
 * or "≥5°F") into the active display unit. The backend hard-codes °F, so in
 * Celsius mode both the boundaries and the suffix would otherwise be wrong.
 * Left untouched in Fahrenheit mode so the °F formatting is preserved exactly.
 */
export function localizeBinLabel(
  label: string,
  toDisplayDelta: (fahrenheitDelta: number) => number,
  unitLabel: string,
  isCelsius: boolean
): string {
  if (!isCelsius) return label;
  const open = label.match(/^≥\s*([\d.]+)/);
  if (open) return `≥${toDisplayDelta(parseFloat(open[1])).toFixed(1)}${unitLabel}`;
  const range = label.match(/^([\d.]+)\s*[–-]\s*([\d.]+)/);
  if (range) {
    const lo = toDisplayDelta(parseFloat(range[1])).toFixed(1);
    const hi = toDisplayDelta(parseFloat(range[2])).toFixed(1);
    return `${lo}–${hi}${unitLabel}`;
  }
  return label;
}

/**
 * Build the degree-minutes area series, scaling each °F·min point by the delta
 * conversion so the magnitude matches the unit label on the axis/tooltip.
 */
export function degreeMinutesSeries(
  series: { period: string; value?: number | null }[] | undefined,
  toDisplayDelta: (fahrenheitDelta: number) => number
): { period: string; value: number }[] {
  return (series ?? []).map((p) => ({
    period: shortDayLabel(p.period),
    value: toDisplayDelta(p.value ?? 0),
  }));
}
