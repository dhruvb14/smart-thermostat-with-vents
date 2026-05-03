import { useEffect, useMemo, useState } from "react";
import {
  downloadMetricsCsv,
  getMetricsHomeSummary,
  getMetricsThermostatSummary,
  getOutsideTempEntity,
  getThermostats,
  setOutsideTempEntity,
  type MetricsRange,
  type MetricsSummary,
  type ThermostatConfig,
} from "../api";
import EntityPicker from "../components/EntityPicker";
import { ChartGrid } from "../components/charts/MetricsCharts";
import { useUnit } from "../contexts";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const HOME = "__home__"; // sentinel value for "All thermostats" in the selector

function isoDate(d: Date): string {
  // Local-date YYYY-MM-DD. Avoids the UTC-shift trap of `toISOString().slice(0,10)`.
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function defaultRange(): MetricsRange {
  const today = new Date();
  const start = new Date(today);
  start.setDate(start.getDate() - 6);
  return { start: isoDate(start), end: isoDate(today) };
}

function formatSeconds(s: number): string {
  if (!s) return "0m";
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  if (h >= 1) return `${h}h ${m}m`;
  return `${m}m`;
}

// ---------------------------------------------------------------------------
// Outside-temperature picker (Phase 3c)
// ---------------------------------------------------------------------------

function OutsideTempPanel({ onChange }: { onChange?: (entityId: string | null) => void }) {
  const { fmtTemp } = useUnit();
  const [current, setCurrent] = useState<{
    entity_id: string | null;
    current_value: number | null;
  } | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const refresh = async () => {
    try {
      const cur = await getOutsideTempEntity();
      setCurrent(cur);
      onChange?.(cur.entity_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    }
  };

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onSelect = async (entity_id: string | null) => {
    setSaving(true);
    setError("");
    try {
      const next = await setOutsideTempEntity(entity_id);
      setCurrent(next);
      onChange?.(next.entity_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      <div className="card-title">Outside temperature source</div>
      <div className="text-sm text-muted" style={{ marginBottom: "1rem" }}>
        Select a Home Assistant entity (sensor or weather) whose numeric state Plenum should record
        at the start and end of every cycle. Used for the heating/cooling vs outdoor-temperature
        analytics.
      </div>

      {error && (
        <div className="badge badge-red" style={{ marginBottom: ".75rem" }}>
          {error}
        </div>
      )}

      <div style={{ display: "flex", gap: ".75rem", alignItems: "center", flexWrap: "wrap" }}>
        <div style={{ flex: "1 1 320px", minWidth: 200 }}>
          <EntityPicker
            domain={["sensor", "weather"]}
            placeholder="Search sensor / weather entities…"
            onSelect={(id) => void onSelect(id)}
          />
        </div>

        {current?.entity_id ? (
          <>
            <span className="badge badge-blue">
              {current.entity_id}
              {current.current_value !== null && ` · ${fmtTemp(current.current_value)}`}
            </span>
            <button
              className="btn btn-secondary"
              onClick={() => void onSelect(null)}
              disabled={saving}
              type="button"
            >
              Clear
            </button>
          </>
        ) : (
          <span className="text-sm text-muted">— None (don&apos;t track outside temperature) —</span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Summary tiles (placeholder until charts arrive in Phase 4)
// ---------------------------------------------------------------------------

function SummaryTile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="card" style={{ minWidth: 160, flex: "1 1 160px" }}>
      <div className="text-sm text-muted">{label}</div>
      <div style={{ fontSize: "1.6rem", fontWeight: 600, marginTop: ".25rem" }}>{value}</div>
      {hint && (
        <div className="text-sm text-muted" style={{ marginTop: ".25rem" }}>
          {hint}
        </div>
      )}
    </div>
  );
}

function SummaryTileSkeleton() {
  return (
    <div className="card" style={{ minWidth: 160, flex: "1 1 160px" }}>
      <div
        className="skeleton-block"
        style={{ width: "60%", height: "0.8rem", marginBottom: "0.5rem" }}
      />
      <div className="skeleton-block" style={{ width: "80%", height: "1.6rem" }} />
    </div>
  );
}

function SummarySection({
  summary,
  loading,
}: {
  summary: MetricsSummary | null;
  loading: boolean;
}) {
  const { fmtTemp } = useUnit();
  if (loading) {
    return (
      <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", marginBottom: "1rem" }}>
        {Array.from({ length: 5 }, (_, i) => (
          <SummaryTileSkeleton key={i} />
        ))}
      </div>
    );
  }
  if (!summary) return null;
  const { heating_seconds, cooling_seconds, cycle_count, completed_count, duty_cycle_pct } =
    summary;
  return (
    <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", marginBottom: "1rem" }}>
      <SummaryTile
        label="Heating time"
        value={formatSeconds(heating_seconds)}
        hint={
          summary.thermostat_count > 1 && summary.thermostat_entity_id === null
            ? `${summary.thermostat_count} thermostats`
            : undefined
        }
      />
      <SummaryTile label="Cooling time" value={formatSeconds(cooling_seconds)} />
      <SummaryTile label="Duty cycle" value={`${duty_cycle_pct.toFixed(1)}%`} />
      <SummaryTile
        label="Cycles"
        value={String(cycle_count)}
        hint={`${completed_count} completed`}
      />
      <SummaryTile
        label="Avg outside temp"
        value={
          summary.avg_outside_temp_at_start !== null
            ? fmtTemp(summary.avg_outside_temp_at_start)
            : "—"
        }
        hint="At cycle start"
      />
    </div>
  );
}

/** Issue #85 Phase 5b — top-of-page banners that surface "no data yet"
 * and "outside-temp entity not configured" so users know what they're
 * looking at before drilling into individual chart empty states. */
function EmptyStateBanners({
  summary,
  outsideEntityConfigured,
  loading,
}: {
  summary: MetricsSummary | null;
  outsideEntityConfigured: boolean;
  loading: boolean;
}) {
  if (loading) return null;
  const noData = summary && summary.cycle_count === 0;
  if (!noData && outsideEntityConfigured) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", marginBottom: "1rem" }}>
      {noData && (
        <div className="card" style={{ borderLeft: "3px solid #f59e0b" }}>
          <strong>No cycle data yet for this range.</strong>{" "}
          <span className="text-muted">
            Plenum starts collecting metrics the moment a cycle runs. Trigger one by enabling a
            schedule or motion-activating a room — the page will populate automatically.
          </span>
        </div>
      )}
      {!outsideEntityConfigured && (
        <div className="card" style={{ borderLeft: "3px solid #3b82f6" }}>
          <strong>Outside-temperature entity not configured.</strong>{" "}
          <span className="text-muted">
            Set one above to unlock the cycles-vs-outside-temperature scatter and the average
            outside-temp summary tile. Cycles still log without it; only the temperature columns
            stay NULL.
          </span>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function Metrics() {
  const [thermostats, setThermostats] = useState<ThermostatConfig[]>([]);
  const [selected, setSelected] = useState<string>(HOME);
  const [range, setRange] = useState<MetricsRange>(defaultRange());
  const [summary, setSummary] = useState<MetricsSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [outsideEntity, setOutsideEntity] = useState<string | null>(null);

  // Load thermostats on mount.
  useEffect(() => {
    getThermostats()
      .then(setThermostats)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load thermostats"));
  }, []);

  // Re-fetch summary whenever the selector or range changes.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    const fetcher =
      selected === HOME
        ? getMetricsHomeSummary(range)
        : getMetricsThermostatSummary(selected, range);
    fetcher
      .then((s) => {
        if (!cancelled) setSummary(s);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load metrics");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected, range]);

  const csvScope = selected === HOME ? "home" : "thermostat";
  const csvEntityId = selected === HOME ? undefined : selected;

  const subtitle = useMemo(() => {
    if (selected === HOME) {
      return `${thermostats.length} thermostat${thermostats.length === 1 ? "" : "s"}`;
    }
    const t = thermostats.find((t) => t.thermostat_entity_id === selected);
    return t?.name || selected;
  }, [selected, thermostats]);

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">Metrics</div>
          <div className="page-subtitle">
            Heating &amp; cooling analytics for the home and individual thermostats. Charts arrive
            in Phase 4 — this scaffold wires the data feed and filters.
          </div>
        </div>
      </div>

      <OutsideTempPanel onChange={setOutsideEntity} />

      <div className="card" style={{ marginBottom: "1rem" }}>
        <div
          style={{
            display: "flex",
            gap: "1rem",
            flexWrap: "wrap",
            alignItems: "flex-end",
          }}
        >
          <div className="form-group" style={{ marginBottom: 0, minWidth: 200, flex: "1 1 240px" }}>
            <label className="form-label">Thermostat</label>
            <select
              className="form-control"
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
            >
              <option value={HOME}>All thermostats (home)</option>
              {thermostats.map((t) => (
                <option key={t.thermostat_entity_id} value={t.thermostat_entity_id}>
                  {t.name || t.thermostat_entity_id}
                </option>
              ))}
            </select>
          </div>
          <div className="form-group" style={{ marginBottom: 0 }}>
            <label className="form-label">Start</label>
            <input
              type="date"
              className="form-control"
              value={range.start ?? ""}
              max={range.end}
              onChange={(e) => setRange((r) => ({ ...r, start: e.target.value }))}
            />
          </div>
          <div className="form-group" style={{ marginBottom: 0 }}>
            <label className="form-label">End</label>
            <input
              type="date"
              className="form-control"
              value={range.end ?? ""}
              min={range.start}
              onChange={(e) => setRange((r) => ({ ...r, end: e.target.value }))}
            />
          </div>
          <div style={{ display: "flex", gap: ".5rem" }}>
            <button
              className="btn btn-secondary"
              onClick={() => setRange(defaultRange())}
              type="button"
            >
              Last 7 days
            </button>
            <button
              className="btn btn-secondary"
              onClick={() => downloadMetricsCsv(range, csvScope, csvEntityId)}
              type="button"
              title="Download cycle log CSV for this range"
            >
              Export CSV
            </button>
          </div>
        </div>
        <div className="text-sm text-muted" style={{ marginTop: ".75rem" }}>
          Showing {subtitle} for {range.start} → {range.end}
        </div>
      </div>

      {error && (
        <div className="badge badge-red" style={{ marginBottom: "1rem" }}>
          {error}
        </div>
      )}

      <SummarySection summary={summary} loading={loading} />

      <EmptyStateBanners
        summary={summary}
        outsideEntityConfigured={!!outsideEntity}
        loading={loading}
      />

      <ChartGrid
        entityId={selected === HOME ? null : selected}
        range={range}
        homeSummary={summary}
        homeLoading={loading}
        isHome={selected === HOME}
      />
    </div>
  );
}
