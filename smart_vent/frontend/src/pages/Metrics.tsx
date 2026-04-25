import { useEffect, useMemo, useState } from "react";
import {
  downloadMetricsCsv,
  getHAEntities,
  getMetricsHomeSummary,
  getMetricsThermostatSummary,
  getOutsideTempEntity,
  getThermostats,
  setOutsideTempEntity,
  type HAEntity,
  type MetricsRange,
  type MetricsSummary,
  type ThermostatConfig,
} from "../api";

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

function OutsideTempPanel() {
  const [entities, setEntities] = useState<HAEntity[]>([]);
  const [current, setCurrent] = useState<{
    entity_id: string | null;
    current_value: number | null;
  } | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const refresh = async () => {
    try {
      const [list, cur] = await Promise.all([
        getHAEntities(["sensor", "weather"]),
        getOutsideTempEntity(),
      ]);
      setEntities(list);
      setCurrent(cur);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  const onSelect = async (entity_id: string | null) => {
    setSaving(true);
    setError("");
    try {
      const next = await setOutsideTempEntity(entity_id);
      setCurrent(next);
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
        analytics. °C entities are converted to °F automatically.
      </div>

      {error && (
        <div className="badge badge-red" style={{ marginBottom: ".75rem" }}>
          {error}
        </div>
      )}

      <div style={{ display: "flex", gap: ".75rem", alignItems: "center", flexWrap: "wrap" }}>
        <select
          className="form-control"
          style={{ flex: "1 1 320px", minWidth: 200 }}
          value={current?.entity_id ?? ""}
          onChange={(e) => onSelect(e.target.value || null)}
          disabled={saving}
        >
          <option value="">— None (don't track outside temperature) —</option>
          {entities.map((e) => (
            <option key={e.entity_id} value={e.entity_id}>
              {e.friendly_name} ({e.entity_id})
            </option>
          ))}
        </select>

        {current?.entity_id && (
          <span className="badge badge-blue">
            Current value:{" "}
            {current.current_value !== null ? `${current.current_value.toFixed(1)} °F` : "n/a"}
          </span>
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

function SummarySection({
  summary,
  loading,
}: {
  summary: MetricsSummary | null;
  loading: boolean;
}) {
  if (loading) {
    return (
      <div className="loading">
        <div className="spinner" /> Loading summary…
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
            ? `${summary.avg_outside_temp_at_start.toFixed(1)} °F`
            : "—"
        }
        hint="At cycle start"
      />
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

      <OutsideTempPanel />

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

      <div className="card">
        <div className="card-title">Charts</div>
        <div className="empty-state" style={{ padding: "2rem 1rem" }}>
          Chart slots will appear here in Phase 4 (heating/cooling hours, cycles, duty cycle, etc.).
          The data feed for every chart is already live — see the API summary panel above.
        </div>
      </div>
    </div>
  );
}
