// Shared chart palette (Issue #85 Phase 4) — keeping colours consistent
// across charts so heating/cooling/etc read the same wherever they appear.

export const COLORS = {
  heating: "#f97316",
  cooling: "#3b82f6",
  duty: "#8b5cf6",
  duration: "#0d9488",
  outside: "#d97706",
  completed: "#16a34a",
  timeout: "#dc2626",
  aborted: "#6b7280",
  schedule: "#3b82f6",
  presence: "#10b981",
  override: "#f59e0b",
  safety: "#e11d48",
  degree: "#7c3aed",
  overshoot: "#ef4444",
  participation: "#0ea5e9",
} as const;

/** Reusable Recharts tooltip styling so every chart's tooltip looks the same. */
export const TOOLTIP_STYLE = {
  background: "var(--bg-elev, #1f2937)",
  border: "1px solid var(--border, #374151)",
  borderRadius: 6,
  fontSize: 12,
  color: "var(--text, #f3f4f6)",
} as const;
