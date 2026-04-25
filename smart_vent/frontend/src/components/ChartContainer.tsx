import type { ReactNode } from "react";
import { ResponsiveContainer } from "recharts";

interface Props {
  title: string;
  subtitle?: string;
  /** Chart height in pixels. Defaults to 300. */
  height?: number;
  /** Slot for a top-right action (e.g. a download button or filter toggle). */
  action?: ReactNode;
  /** Disclosure note shown beneath the chart (e.g. for vent timeline). */
  note?: ReactNode;
  /** Empty-state message displayed when there's no data to plot. */
  empty?: boolean;
  emptyText?: string;
  loading?: boolean;
  children: ReactNode;
}

/**
 * Reusable chart frame with consistent card styling, title, optional
 * subtitle/action slot, loading + empty states, and disclosure footer.
 * (Issue #85 Phase 3b — every chart in Phase 4 should use this container
 * so visuals stay aligned without each chart re-implementing the chrome.)
 */
export default function ChartContainer({
  title,
  subtitle,
  height = 300,
  action,
  note,
  empty,
  emptyText,
  loading,
  children,
}: Props) {
  return (
    <div className="card chart-card">
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          gap: "1rem",
          marginBottom: ".75rem",
        }}
      >
        <div>
          <div className="card-title" style={{ marginBottom: subtitle ? ".15rem" : 0 }}>
            {title}
          </div>
          {subtitle && <div className="text-sm text-muted">{subtitle}</div>}
        </div>
        {action && <div>{action}</div>}
      </div>

      {loading ? (
        <div className="loading" style={{ minHeight: height }}>
          <div className="spinner" /> Loading…
        </div>
      ) : empty ? (
        <div
          className="empty-state"
          style={{
            minHeight: height,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          {emptyText ?? "No data for this range yet."}
        </div>
      ) : (
        <div style={{ width: "100%", height }}>
          <ResponsiveContainer width="100%" height="100%">
            {children as React.ReactElement}
          </ResponsiveContainer>
        </div>
      )}

      {note && (
        <div className="text-sm text-muted" style={{ marginTop: ".75rem" }}>
          {note}
        </div>
      )}
    </div>
  );
}
