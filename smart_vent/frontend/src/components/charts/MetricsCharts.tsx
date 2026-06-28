/**
 * Issue #85 Phase 4 — all 14 metrics charts.
 *
 * Each chart is a small self-contained component that takes the active
 * thermostat entity_id and the date range, fetches its own data, and
 * renders inside a shared <ChartContainer/>. Composed in pages/Metrics.tsx.
 */

import { useEffect, useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import {
  getMetricsCyclesVsOutsideTemp,
  getMetricsHourHeatmap,
  getMetricsOvershootHistogram,
  getMetricsRoomBreakdown,
  getMetricsThermostatSummary,
  getMetricsTimeseries,
  getMetricsVentTimeline,
  type CyclesVsOutsideTempPoint,
  type HourHeatmap,
  type MetricsRange,
  type MetricsSummary,
  type MetricsTimeseries,
  type OvershootHistogram,
  type RoomMetric,
  type VentTimelineEvent,
} from "../../api";
import ChartContainer from "../ChartContainer";
import { COLORS, TOOLTIP_STYLE } from "./colors";
import {
  fmtMinutes,
  fmtPercent,
  fmtSecondsAsHm,
  shortDayLabel,
  fmtOvershootDelta,
  localizeBinLabel,
  degreeMinutesSeries,
} from "./format";
import { useUnit } from "../../contexts";

interface Props {
  entityId: string;
  range: MetricsRange;
}

// Tiny hook that wraps a fetcher into {data, loading, error} and re-runs
// when `deps` change. Avoids adding a hook lib.
function useFetch<T>(fetcher: () => Promise<T>, deps: React.DependencyList) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    fetcher()
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, loading, error };
}

// ---------------------------------------------------------------------------
// 4a — Heating/cooling hours per day (stacked bar)
// ---------------------------------------------------------------------------

export function HeatingCoolingHoursChart({ entityId, range }: Props) {
  const { data, loading } = useFetch<MetricsTimeseries>(
    () => getMetricsTimeseries(entityId, "hours", "day", range),
    [entityId, range.start, range.end]
  );
  const series = useMemo(
    () =>
      (data?.series ?? []).map((p) => ({
        period: shortDayLabel(p.period),
        heating: (p.heating_seconds ?? 0) / 3600,
        cooling: (p.cooling_seconds ?? 0) / 3600,
      })),
    [data]
  );
  return (
    <ChartContainer
      title="Heating &amp; cooling hours per day"
      subtitle="Stacked bars — total HVAC run-time bucketed by local date."
      loading={loading}
      empty={series.length === 0}
    >
      <BarChart data={series}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="period" />
        <YAxis tickFormatter={(v) => `${v}h`} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          formatter={(v: unknown) => `${Number(v).toFixed(2)}h`}
        />
        <Legend />
        <Bar dataKey="heating" stackId="a" fill={COLORS.heating} name="Heating" />
        <Bar dataKey="cooling" stackId="a" fill={COLORS.cooling} name="Cooling" />
      </BarChart>
    </ChartContainer>
  );
}

// ---------------------------------------------------------------------------
// 4b — Cycles per day (bar)
// ---------------------------------------------------------------------------

export function CyclesPerDayChart({ entityId, range }: Props) {
  const { data, loading } = useFetch<MetricsTimeseries>(
    () => getMetricsTimeseries(entityId, "cycles", "day", range),
    [entityId, range.start, range.end]
  );
  const series = (data?.series ?? []).map((p) => ({
    period: shortDayLabel(p.period),
    value: p.value ?? 0,
  }));
  return (
    <ChartContainer
      title="Cycles per day"
      subtitle="How many distinct heating/cooling cycles ran each day."
      loading={loading}
      empty={series.length === 0}
    >
      <BarChart data={series}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="period" />
        <YAxis allowDecimals={false} />
        <Tooltip contentStyle={TOOLTIP_STYLE} />
        <Bar dataKey="value" fill={COLORS.cooling} name="Cycles" />
      </BarChart>
    </ChartContainer>
  );
}

// ---------------------------------------------------------------------------
// 4c — Average cycle duration (line)
// ---------------------------------------------------------------------------

export function AvgCycleDurationChart({ entityId, range }: Props) {
  const { data, loading } = useFetch<MetricsTimeseries>(
    () => getMetricsTimeseries(entityId, "avg_duration", "day", range),
    [entityId, range.start, range.end]
  );
  const series = (data?.series ?? []).map((p) => ({
    period: shortDayLabel(p.period),
    minutes: p.value != null ? p.value / 60 : null,
  }));
  return (
    <ChartContainer
      title="Average cycle duration"
      subtitle="Mean cycle length per day, in minutes."
      loading={loading}
      empty={series.length === 0}
    >
      <LineChart data={series}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="period" />
        <YAxis tickFormatter={(v) => `${v}m`} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          formatter={(v: unknown) => `${Number(v).toFixed(1)} min`}
        />
        <Line
          type="monotone"
          dataKey="minutes"
          stroke={COLORS.duration}
          strokeWidth={2}
          dot={false}
          connectNulls
        />
      </LineChart>
    </ChartContainer>
  );
}

// ---------------------------------------------------------------------------
// 4d — Cycles vs outside temperature (scatter)
// ---------------------------------------------------------------------------

export function CyclesVsOutsideTempChart({ entityId, range }: Props) {
  const { unitLabel, toDisplay } = useUnit();
  const { data, loading } = useFetch<{ points: CyclesVsOutsideTempPoint[] }>(
    () => getMetricsCyclesVsOutsideTemp(entityId, range),
    [entityId, range.start, range.end]
  );
  const allPoints = (data?.points ?? []).map((p) => ({
    ...p,
    outside_temp: p.outside_temp != null ? toDisplay(p.outside_temp) : p.outside_temp,
  }));
  const heating = allPoints.filter((p) => p.mode === "heating");
  const cooling = allPoints.filter((p) => p.mode === "cooling");
  const empty = !data || data.points.length === 0;
  return (
    <ChartContainer
      title="Cycles vs outside temperature"
      subtitle={`Each dot = one cycle. X = outside ${unitLabel} at start, Y = duration (min).`}
      loading={loading}
      empty={empty}
      emptyText="No outside-temp data yet — configure the outside-temperature entity at the top of the page."
    >
      <ScatterChart>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis
          type="number"
          dataKey="outside_temp"
          name={`Outside ${unitLabel}`}
          unit={unitLabel}
          domain={["auto", "auto"]}
        />
        <YAxis type="number" dataKey="duration_minutes" name="Duration (min)" unit="m" />
        <ZAxis range={[60, 60]} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          cursor={{ strokeDasharray: "3 3" }}
          formatter={(v: unknown) => Number(v).toFixed(1)}
        />
        <Legend />
        <Scatter name="Heating" data={heating} fill={COLORS.heating} />
        <Scatter name="Cooling" data={cooling} fill={COLORS.cooling} />
      </ScatterChart>
    </ChartContainer>
  );
}

// ---------------------------------------------------------------------------
// 4e — Duty cycle % (line)
// ---------------------------------------------------------------------------

export function DutyCycleChart({ entityId, range }: Props) {
  const { data, loading } = useFetch<MetricsTimeseries>(
    () => getMetricsTimeseries(entityId, "duty_cycle", "day", range),
    [entityId, range.start, range.end]
  );
  const series = (data?.series ?? []).map((p) => ({
    period: shortDayLabel(p.period),
    value: p.value ?? 0,
  }));
  return (
    <ChartContainer
      title="Duty cycle"
      subtitle="HVAC run-time as a percentage of each day."
      loading={loading}
      empty={series.length === 0}
    >
      <LineChart data={series}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="period" />
        <YAxis unit="%" />
        <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v: unknown) => fmtPercent(Number(v))} />
        <Line type="monotone" dataKey="value" stroke={COLORS.duty} strokeWidth={2} dot={false} />
      </LineChart>
    </ChartContainer>
  );
}

// ---------------------------------------------------------------------------
// 4f — Time-to-target (line, minutes)
// ---------------------------------------------------------------------------

export function TimeToTargetChart({ entityId, range }: Props) {
  const { data, loading } = useFetch<MetricsTimeseries>(
    () => getMetricsTimeseries(entityId, "time_to_target", "day", range),
    [entityId, range.start, range.end]
  );
  const series = (data?.series ?? []).map((p) => ({
    period: shortDayLabel(p.period),
    minutes: p.value != null ? p.value / 60 : null,
  }));
  return (
    <ChartContainer
      title="Time to target"
      subtitle="Average minutes from cycle start to first room reaching target."
      loading={loading}
      empty={series.length === 0}
    >
      <LineChart data={series}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="period" />
        <YAxis tickFormatter={(v) => `${v}m`} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          formatter={(v: unknown) => fmtMinutes(Number(v) * 60)}
        />
        <Line
          type="monotone"
          dataKey="minutes"
          stroke={COLORS.duration}
          strokeWidth={2}
          dot={false}
          connectNulls
        />
      </LineChart>
    </ChartContainer>
  );
}

// ---------------------------------------------------------------------------
// 4g — Cycle completion rate (donut)
// ---------------------------------------------------------------------------

export function CompletionRateChart({
  summary,
  loading,
}: {
  summary: MetricsSummary | null;
  loading: boolean;
}) {
  const data = summary
    ? [
        { name: "Completed", value: summary.completed_count, color: COLORS.completed },
        { name: "Timeout", value: summary.timeout_count, color: COLORS.timeout },
        { name: "Aborted", value: summary.aborted_count, color: COLORS.aborted },
      ].filter((d) => d.value > 0)
    : [];
  const total = data.reduce((s, d) => s + d.value, 0);
  return (
    <ChartContainer
      title="Cycle completion rate"
      subtitle={
        total
          ? `${total} cycles — ${fmtPercent(((summary?.completed_count ?? 0) / total) * 100)} completed`
          : undefined
      }
      loading={loading}
      empty={total === 0}
    >
      <PieChart>
        <Tooltip contentStyle={TOOLTIP_STYLE} />
        <Legend />
        <Pie
          data={data}
          dataKey="value"
          nameKey="name"
          innerRadius={50}
          outerRadius={80}
          paddingAngle={2}
        >
          {data.map((d, i) => (
            <Cell key={i} fill={d.color} />
          ))}
        </Pie>
      </PieChart>
    </ChartContainer>
  );
}

// ---------------------------------------------------------------------------
// 4h — Source breakdown (donut)
// ---------------------------------------------------------------------------

export function SourceBreakdownChart({
  summary,
  loading,
}: {
  summary: MetricsSummary | null;
  loading: boolean;
}) {
  const sources = summary?.source_breakdown ?? {};
  const data = Object.entries(sources)
    .map(([name, value]) => ({
      name: name.charAt(0).toUpperCase() + name.slice(1),
      value: value as number,
      color:
        name === "schedule"
          ? COLORS.schedule
          : name === "presence"
            ? COLORS.presence
            : name === "safety"
              ? COLORS.safety
              : COLORS.override,
    }))
    .filter((d) => d.value > 0);
  return (
    <ChartContainer
      title="Source breakdown"
      subtitle="Which trigger started each cycle (counts cycles where each source was active for at least one room)."
      loading={loading}
      empty={data.length === 0}
    >
      <PieChart>
        <Tooltip contentStyle={TOOLTIP_STYLE} />
        <Legend />
        <Pie
          data={data}
          dataKey="value"
          nameKey="name"
          innerRadius={50}
          outerRadius={80}
          paddingAngle={2}
        >
          {data.map((d, i) => (
            <Cell key={i} fill={d.color} />
          ))}
        </Pie>
      </PieChart>
    </ChartContainer>
  );
}

// ---------------------------------------------------------------------------
// 4i — Per-room heating vs cooling time (stacked horizontal bar)
// ---------------------------------------------------------------------------

export function PerRoomHeatingCoolingChart({ entityId, range }: Props) {
  const { data, loading } = useFetch<{ rooms: RoomMetric[] }>(
    () => getMetricsRoomBreakdown(entityId, range),
    [entityId, range.start, range.end]
  );
  const series = (data?.rooms ?? []).map((r) => ({
    name: r.room_name,
    heating: r.heating_seconds / 3600,
    cooling: r.cooling_seconds / 3600,
  }));
  return (
    <ChartContainer
      title="Per-room heating vs cooling"
      subtitle="Total hours each room was actively cooled or heated."
      loading={loading}
      empty={series.length === 0}
    >
      <BarChart data={series} layout="vertical">
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis type="number" tickFormatter={(v) => `${v}h`} />
        <YAxis dataKey="name" type="category" width={120} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          formatter={(v: unknown) => `${Number(v).toFixed(2)}h`}
        />
        <Legend />
        <Bar dataKey="heating" stackId="a" fill={COLORS.heating} name="Heating" />
        <Bar dataKey="cooling" stackId="a" fill={COLORS.cooling} name="Cooling" />
      </BarChart>
    </ChartContainer>
  );
}

// ---------------------------------------------------------------------------
// 4j — Room participation rate (horizontal bar, %)
// ---------------------------------------------------------------------------

export function RoomParticipationChart({ entityId, range }: Props) {
  const { data, loading } = useFetch<{ rooms: RoomMetric[] }>(
    () => getMetricsRoomBreakdown(entityId, range),
    [entityId, range.start, range.end]
  );
  const series = (data?.rooms ?? []).map((r) => ({
    name: r.room_name,
    pct: Math.round(r.participation_rate * 100),
    count: r.participation_count,
  }));
  return (
    <ChartContainer
      title="Room participation rate"
      subtitle="Percentage of cycles in which each room was an active participant."
      loading={loading}
      empty={series.length === 0}
    >
      <BarChart data={series} layout="vertical">
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis type="number" domain={[0, 100]} tickFormatter={(v) => `${v}%`} />
        <YAxis dataKey="name" type="category" width={120} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          formatter={(_v: unknown, _n: unknown, p: unknown) => {
            const item = p as { payload?: { pct?: number; count?: number } };
            return `${item.payload?.pct ?? 0}% (${item.payload?.count ?? 0} cycles)`;
          }}
        />
        <Bar dataKey="pct" fill={COLORS.participation} name="Participation %" />
      </BarChart>
    </ChartContainer>
  );
}

// ---------------------------------------------------------------------------
// 4k — Degree-minutes (area)
// ---------------------------------------------------------------------------

export function DegreeMinutesChart({ entityId, range }: Props) {
  const { unitLabel, toDisplayDelta } = useUnit();
  const { data, loading } = useFetch<MetricsTimeseries>(
    () => getMetricsTimeseries(entityId, "degree_minutes", "day", range),
    [entityId, range.start, range.end]
  );
  const series = degreeMinutesSeries(data?.series, toDisplayDelta);
  return (
    <ChartContainer
      title="Degree-minutes"
      subtitle="∫ |setpoint − thermostat temperature| dt — a single load proxy. Lower = closer to setpoint."
      loading={loading}
      empty={series.length === 0}
    >
      <AreaChart data={series}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="period" />
        <YAxis />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          formatter={(v: unknown) => `${Number(v).toFixed(1)} ${unitLabel}·min`}
        />
        <Area
          type="monotone"
          dataKey="value"
          stroke={COLORS.degree}
          fill={COLORS.degree}
          fillOpacity={0.3}
          strokeWidth={2}
        />
      </AreaChart>
    </ChartContainer>
  );
}

// ---------------------------------------------------------------------------
// 4l — Overshoot histogram
// ---------------------------------------------------------------------------

export function OvershootHistogramChart({ entityId, range }: Props) {
  const { isCelsius, toDisplayDelta, unitLabel } = useUnit();
  const { data, loading } = useFetch<OvershootHistogram>(
    () => getMetricsOvershootHistogram(entityId, range),
    [entityId, range.start, range.end]
  );
  const series = (data?.labels ?? []).map((label, i) => ({
    label: localizeBinLabel(label, toDisplayDelta, unitLabel, isCelsius),
    count: data?.counts?.[i] ?? 0,
  }));
  const subtitle = data
    ? `${data.overshot_count}/${data.total_room_cycles} room-cycles overshot — max ${fmtOvershootDelta(data.max_overshoot_f, toDisplayDelta, unitLabel)}, avg ${fmtOvershootDelta(data.avg_overshoot_f, toDisplayDelta, unitLabel)}`
    : undefined;
  return (
    <ChartContainer
      title="Overshoot histogram"
      subtitle={subtitle}
      loading={loading}
      empty={!data || data.total_room_cycles === 0}
    >
      <BarChart data={series}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="label" />
        <YAxis allowDecimals={false} />
        <Tooltip contentStyle={TOOLTIP_STYLE} />
        <Bar dataKey="count" fill={COLORS.overshoot} name="Room-cycles" />
      </BarChart>
    </ChartContainer>
  );
}

// ---------------------------------------------------------------------------
// 4m — Hour-of-day heatmap (7×24 grid, rendered with plain CSS — recharts
// doesn't ship a heatmap)
// ---------------------------------------------------------------------------

export function HourHeatmapChart({ entityId, range }: Props) {
  const { data, loading } = useFetch<HourHeatmap>(
    () => getMetricsHourHeatmap(entityId, range),
    [entityId, range.start, range.end]
  );

  const max = useMemo(() => {
    if (!data) return 0;
    let m = 0;
    for (const row of data.grid_seconds) for (const v of row) if (v > m) m = v;
    return m;
  }, [data]);

  const cellColor = (secs: number) => {
    if (max === 0) return "var(--bg-muted, #1f2937)";
    const t = Math.min(1, secs / max);
    const alpha = 0.08 + t * 0.92;
    return `rgba(139, 92, 246, ${alpha})`;
  };

  return (
    <div className="card chart-card">
      <div className="card-title" style={{ marginBottom: ".15rem" }}>
        Hour-of-day heatmap
      </div>
      <div className="text-sm text-muted" style={{ marginBottom: ".75rem" }}>
        Total HVAC seconds per (day-of-week × hour) cell across the selected range.
      </div>
      {loading ? (
        <div className="loading">
          <div className="spinner" /> Loading…
        </div>
      ) : !data || max === 0 ? (
        <div className="empty-state">No HVAC activity recorded for this range.</div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse", fontSize: 11 }}>
            <thead>
              <tr>
                <th style={{ padding: "2px 4px" }} />
                {Array.from({ length: 24 }, (_, h) => (
                  <th key={h} style={{ padding: "2px 4px", fontWeight: 400, opacity: 0.7 }}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.grid_seconds.map((row, dow) => (
                <tr key={dow}>
                  <td
                    style={{
                      padding: "2px 6px",
                      fontWeight: 500,
                      opacity: 0.8,
                      textAlign: "right",
                    }}
                  >
                    {data.day_labels[dow]}
                  </td>
                  {row.map((secs, h) => (
                    <td
                      key={h}
                      title={`${data.day_labels[dow]} ${h}:00 — ${fmtSecondsAsHm(secs)}`}
                      style={{
                        width: 18,
                        height: 18,
                        background: cellColor(secs),
                        border: "1px solid rgba(255,255,255,0.05)",
                      }}
                    />
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 4n — Vent timeline (cycle-boundary events, with disclosure note)
// ---------------------------------------------------------------------------

export function VentTimelineChart({ entityId, range }: Props) {
  const { data, loading } = useFetch<{ events: VentTimelineEvent[]; note: string }>(
    () => getMetricsVentTimeline(entityId, range),
    [entityId, range.start, range.end]
  );
  const events = data?.events ?? [];

  // Render as a simple table — recharts has no good "timeline of bars" primitive,
  // and a bar-per-event chart doesn't read well at 7-day scale. Phase 5 polish
  // can replace this with a proper Gantt if needed.
  return (
    <div className="card chart-card">
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          gap: "1rem",
          marginBottom: ".5rem",
        }}
      >
        <div>
          <div className="card-title" style={{ marginBottom: ".15rem" }}>
            Vent timeline
          </div>
          <div className="text-sm text-muted">
            Cycle-boundary vent events for the range, in chronological order.
          </div>
        </div>
      </div>
      {loading ? (
        <div className="loading">
          <div className="spinner" /> Loading…
        </div>
      ) : events.length === 0 ? (
        <div className="empty-state">No vent events recorded in this range.</div>
      ) : (
        <div style={{ maxHeight: 320, overflowY: "auto" }}>
          <table className="data-table" style={{ width: "100%", fontSize: 13 }}>
            <thead>
              <tr>
                <th style={{ textAlign: "left", padding: "4px 8px" }}>When</th>
                <th style={{ textAlign: "left", padding: "4px 8px" }}>Mode</th>
                <th style={{ textAlign: "left", padding: "4px 8px" }}>Vent</th>
                <th style={{ textAlign: "left", padding: "4px 8px" }}>Action</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e, i) => (
                <tr key={i}>
                  <td style={{ padding: "4px 8px", whiteSpace: "nowrap" }}>
                    {/* Backend timestamps are naive-UTC; append "Z" so they're
                        parsed as UTC and shown in local time (matches Logs/
                        DevMode), not the raw timezone-shifted ISO string. (#301) */}
                    {new Date(e.timestamp + "Z").toLocaleString()}
                  </td>
                  <td style={{ padding: "4px 8px" }}>
                    <span
                      className="badge"
                      style={{
                        background: e.cycle_mode === "heating" ? COLORS.heating : COLORS.cooling,
                        color: "#fff",
                      }}
                    >
                      {e.cycle_mode}
                    </span>
                  </td>
                  <td style={{ padding: "4px 8px", fontFamily: "monospace" }}>{e.entity_id}</td>
                  <td style={{ padding: "4px 8px" }}>{e.action}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data?.note && (
        <div className="text-sm text-muted" style={{ marginTop: ".75rem" }}>
          {data.note}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helper that selects the right chart set based on whether the user is
// looking at a specific thermostat or the home view. Home view only has
// the two donuts (which the summary endpoint provides for the aggregate).
// ---------------------------------------------------------------------------

export function ChartGrid({
  entityId,
  range,
  homeSummary,
  homeLoading,
  isHome,
}: {
  entityId: string | null;
  range: MetricsRange;
  homeSummary: MetricsSummary | null;
  homeLoading: boolean;
  isHome: boolean;
}) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(min(380px, 100%), 1fr))",
        gap: "1rem",
      }}
    >
      {isHome ? (
        <>
          <CompletionRateChart summary={homeSummary} loading={homeLoading} />
          <SourceBreakdownChart summary={homeSummary} loading={homeLoading} />
        </>
      ) : entityId ? (
        <PerThermostatCharts entityId={entityId} range={range} />
      ) : null}
    </div>
  );
}

function PerThermostatCharts({ entityId, range }: { entityId: string; range: MetricsRange }) {
  const { data: summary, loading: sLoading } = useFetch<MetricsSummary>(
    () => getMetricsThermostatSummary(entityId, range),
    [entityId, range.start, range.end]
  );
  return (
    <>
      <HeatingCoolingHoursChart entityId={entityId} range={range} />
      <CyclesPerDayChart entityId={entityId} range={range} />
      <AvgCycleDurationChart entityId={entityId} range={range} />
      <DutyCycleChart entityId={entityId} range={range} />
      <TimeToTargetChart entityId={entityId} range={range} />
      <CompletionRateChart summary={summary} loading={sLoading} />
      <SourceBreakdownChart summary={summary} loading={sLoading} />
      <CyclesVsOutsideTempChart entityId={entityId} range={range} />
      <DegreeMinutesChart entityId={entityId} range={range} />
      <OvershootHistogramChart entityId={entityId} range={range} />
      <PerRoomHeatingCoolingChart entityId={entityId} range={range} />
      <RoomParticipationChart entityId={entityId} range={range} />
      <HourHeatmapChart entityId={entityId} range={range} />
      <VentTimelineChart entityId={entityId} range={range} />
    </>
  );
}
