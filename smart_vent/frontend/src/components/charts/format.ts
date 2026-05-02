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
