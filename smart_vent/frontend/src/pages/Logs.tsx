import { useEffect, useRef, useState } from "react";
import {
  getLogs, getEventLogs, clearEventLogs, getLogRetention, setLogRetention,
  connectWS,
  type CycleLog, type EventLogEntry, type LogRetentionSettings,
} from "../api";

// ---------------------------------------------------------------------------
// Shared: time-window helpers
// ---------------------------------------------------------------------------

type TimePreset = "1h" | "6h" | "24h" | "7d" | "custom";

const PRESETS: { label: string; value: TimePreset }[] = [
  { label: "1h",  value: "1h"  },
  { label: "6h",  value: "6h"  },
  { label: "24h", value: "24h" },
  { label: "7d",  value: "7d"  },
  { label: "Custom", value: "custom" },
];

function presetToSince(preset: TimePreset): string | undefined {
  if (preset === "custom") return undefined;
  const ms: Record<string, number> = { "1h": 1, "6h": 6, "24h": 24, "7d": 168 };
  const d = new Date();
  d.setHours(d.getHours() - ms[preset]);
  return d.toISOString();
}

// ---------------------------------------------------------------------------
// Shared: confirmation modal
// ---------------------------------------------------------------------------

function ConfirmModal({
  title,
  message,
  confirmLabel = "Confirm",
  onConfirm,
  onCancel,
}: {
  title: string;
  message: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="modal-backdrop" onClick={e => e.target === e.currentTarget && onCancel()}>
      <div className="modal" style={{ maxWidth: 440 }}>
        <div className="modal-title">{title}</div>
        <p style={{ color: "var(--gray-700)", marginBottom: "1.5rem" }}>{message}</p>
        <div className="modal-footer">
          <button className="btn btn-secondary" onClick={onCancel}>Cancel</button>
          <button className="btn btn-danger" onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shared: time-window controls
// ---------------------------------------------------------------------------

function TimeWindowControls({
  preset,
  customFrom,
  customTo,
  onPreset,
  onCustomFrom,
  onCustomTo,
}: {
  preset: TimePreset;
  customFrom: string;
  customTo: string;
  onPreset: (p: TimePreset) => void;
  onCustomFrom: (v: string) => void;
  onCustomTo: (v: string) => void;
}) {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: ".4rem", alignItems: "center" }}>
      <span className="text-sm text-muted" style={{ marginRight: ".25rem" }}>Window:</span>
      {PRESETS.map(p => (
        <button
          key={p.value}
          className={`btn btn-sm ${preset === p.value ? "btn-primary" : "btn-secondary"}`}
          onClick={() => onPreset(p.value)}
        >
          {p.label}
        </button>
      ))}
      {preset === "custom" && (
        <>
          <input
            type="datetime-local"
            className="form-control form-control-sm"
            style={{ width: "auto" }}
            value={customFrom}
            onChange={e => onCustomFrom(e.target.value)}
          />
          <span className="text-sm text-muted">to</span>
          <input
            type="datetime-local"
            className="form-control form-control-sm"
            style={{ width: "auto" }}
            value={customTo}
            onChange={e => onCustomTo(e.target.value)}
          />
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cycle History tab
// ---------------------------------------------------------------------------

function duration(start: string, end: string | null): string {
  if (!end) return "running";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  const mins = Math.round(ms / 60000);
  if (mins < 60) return `${mins}m`;
  return `${(mins / 60).toFixed(1)}h`;
}

function LogRow({ log }: { log: CycleLog }) {
  const [expanded, setExpanded] = useState(false);
  const rooms = Object.values(log.rooms);

  return (
    <>
      <tr style={{ cursor: "pointer" }} onClick={() => setExpanded(e => !e)}>
        <td className="font-mono" style={{ fontSize: ".75rem" }}>{log.id.slice(0, 8)}…</td>
        <td className="font-mono" style={{ fontSize: ".8rem" }}>{log.thermostat_entity_id}</td>
        <td>
          <span className={`badge badge-${log.mode === "cooling" ? "blue" : "orange"}`}>{log.mode}</span>
        </td>
        <td>{new Date(log.started_at + "Z").toLocaleString()}</td>
        <td>{log.ended_at ? new Date(log.ended_at + "Z").toLocaleString() : <span className="badge badge-green">Active</span>}</td>
        <td>{duration(log.started_at, log.ended_at)}</td>
        <td>{rooms.length}</td>
        <td>{expanded ? "▲" : "▼"}</td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={8} style={{ background: "var(--gray-50)", padding: ".75rem 1rem" }}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: ".5rem" }}>
              {rooms.map((r, i) => (
                <div key={i} className="card" style={{ padding: ".75rem" }}>
                  <div style={{ fontWeight: 600, marginBottom: ".25rem" }}>{r.name}</div>
                  <div className="text-sm text-muted">Target: {r.target}°F</div>
                  <div className="text-sm text-muted">Source: {r.source}</div>
                </div>
              ))}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

const PAGE_SIZE = 50;

function CycleHistory() {
  const [logs, setLogs] = useState<CycleLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const [offset, setOffset] = useState(0);

  const [preset, setPreset] = useState<TimePreset>("24h");
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");

  const buildParams = (currentOffset: number) => {
    const since = preset !== "custom" ? presetToSince(preset) : (customFrom ? new Date(customFrom).toISOString() : undefined);
    const until = preset === "custom" && customTo ? new Date(customTo).toISOString() : undefined;
    return { limit: PAGE_SIZE, offset: currentOffset, since, until };
  };

  const load = async (reset = false) => {
    setLoading(true);
    const nextOffset = reset ? 0 : offset;
    const rows = await getLogs(buildParams(nextOffset));
    if (reset) {
      setLogs(rows);
      setOffset(rows.length);
    } else {
      setLogs(prev => [...prev, ...rows]);
      setOffset(prev => prev + rows.length);
    }
    setHasMore(rows.length === PAGE_SIZE);
    setLoading(false);
  };

  useEffect(() => { load(true); }, [preset, customFrom, customTo]);

  return (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: ".5rem", marginBottom: "1rem", alignItems: "center" }}>
        <TimeWindowControls
          preset={preset} customFrom={customFrom} customTo={customTo}
          onPreset={p => { setPreset(p); }}
          onCustomFrom={setCustomFrom}
          onCustomTo={setCustomTo}
        />
        <button className="btn btn-secondary btn-sm" onClick={() => load(true)}>↻ Refresh</button>
      </div>

      {loading && logs.length === 0 ? (
        <div className="loading"><div className="spinner" /> Loading logs…</div>
      ) : logs.length === 0 ? (
        <div className="card"><div className="empty-state"><p>No cycle logs in this time window.</p></div></div>
      ) : (
        <div className="card">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ID</th><th>Thermostat</th><th>Mode</th>
                  <th>Started</th><th>Ended</th><th>Duration</th><th>Rooms</th><th></th>
                </tr>
              </thead>
              <tbody>
                {logs.map(l => <LogRow key={l.id} log={l} />)}
              </tbody>
            </table>
          </div>
          {hasMore && (
            <div style={{ padding: ".75rem 1rem", borderTop: "1px solid var(--gray-200)" }}>
              <button className="btn btn-secondary btn-sm" onClick={() => load(false)} disabled={loading}>
                {loading ? "Loading…" : "Load more"}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Live Feed tab
// ---------------------------------------------------------------------------

const LEVEL_COLORS: Record<string, string> = {
  info: "var(--blue)",
  warning: "var(--orange)",
  error: "var(--red)",
};

const CATEGORIES = ["all", "system", "api", "engine", "presence", "ha", "dev", "reconcile"];
const ALL_LEVELS = ["info", "warning", "error"];

function EventEntry({ entry }: { entry: EventLogEntry }) {
  const [expanded, setExpanded] = useState(false);
  const ts = new Date(entry.timestamp + "Z").toLocaleTimeString();
  const color = LEVEL_COLORS[entry.level] ?? "var(--gray-600)";

  return (
    <div
      className="event-entry"
      onClick={() => entry.details && setExpanded(e => !e)}
      style={{ cursor: entry.details ? "pointer" : "default" }}
    >
      <span className="event-ts">{ts}</span>
      <span className="event-level" style={{ color }}>{entry.level.toUpperCase()}</span>
      <span className="event-category">[{entry.category}]</span>
      <span className="event-msg">{entry.message}</span>
      {entry.details && <span className="event-expand">{expanded ? "▲" : "▼"}</span>}
      {expanded && entry.details && (
        <pre className="event-details">{JSON.stringify(entry.details, null, 2)}</pre>
      )}
    </div>
  );
}

function LiveFeed() {
  const [entries, setEntries] = useState<EventLogEntry[]>([]);
  const [category, setCategory] = useState("all");
  const [levels, setLevels] = useState<string[]>(ALL_LEVELS);
  const [paused, setPaused] = useState(false);
  const [loading, setLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const [offset, setOffset] = useState(0);
  const [showClearModal, setShowClearModal] = useState(false);
  const [clearing, setClearing] = useState(false);

  const [preset, setPreset] = useState<TimePreset>("1h");
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");

  const bottomRef = useRef<HTMLDivElement>(null);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  const buildParams = (currentOffset: number) => {
    const since = preset !== "custom" ? presetToSince(preset) : (customFrom ? new Date(customFrom).toISOString() : undefined);
    const until = preset === "custom" && customTo ? new Date(customTo).toISOString() : undefined;
    return {
      limit: PAGE_SIZE,
      offset: currentOffset,
      category: category !== "all" ? category : undefined,
      since,
      until,
      levels: levels.length < ALL_LEVELS.length ? levels : undefined,
    };
  };

  const load = async (reset = false) => {
    setLoading(true);
    const nextOffset = reset ? 0 : offset;
    const rows = await getEventLogs(buildParams(nextOffset));
    // API returns newest-first; reverse so feed shows oldest-at-top
    const ordered = [...rows].reverse();
    if (reset) {
      setEntries(ordered);
      setOffset(rows.length);
    } else {
      // Load more appends older entries at the top
      setEntries(prev => [...ordered, ...prev]);
      setOffset(prev => prev + rows.length);
    }
    setHasMore(rows.length === PAGE_SIZE);
    setLoading(false);
  };

  useEffect(() => { load(true); }, [category, levels, preset, customFrom, customTo]);

  // WebSocket: append new events in real time (unless paused or filtered out)
  useEffect(() => {
    const cleanup = connectWS((event) => {
      if (event.type === "log_event" && !pausedRef.current) {
        const entry = event.data as unknown as EventLogEntry;
        const catOk = category === "all" || entry.category === category;
        const lvlOk = levels.includes(entry.level);
        if (catOk && lvlOk) {
          setEntries(prev => [...prev.slice(-499), entry]);
        }
      }
    });
    return cleanup;
  }, [category, levels]);

  // Auto-scroll to bottom when entries change
  useEffect(() => {
    if (!paused) bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [entries, paused]);

  const toggleLevel = (lv: string) =>
    setLevels(prev => prev.includes(lv) ? prev.filter(l => l !== lv) : [...prev, lv]);

  const handleClear = async () => {
    setClearing(true);
    try {
      await clearEventLogs();
      setEntries([]);
      setOffset(0);
      setHasMore(false);
    } finally {
      setClearing(false);
      setShowClearModal(false);
    }
  };

  if (loading && entries.length === 0) {
    return <div className="loading"><div className="spinner" /> Loading events…</div>;
  }

  return (
    <div>
      {/* Filter bar */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: ".5rem", marginBottom: ".75rem", alignItems: "center" }}>
        <TimeWindowControls
          preset={preset} customFrom={customFrom} customTo={customTo}
          onPreset={p => { setPreset(p); }}
          onCustomFrom={setCustomFrom}
          onCustomTo={setCustomTo}
        />
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: ".5rem", marginBottom: ".75rem", alignItems: "center" }}>
        {/* Category */}
        <select
          className="form-control form-control-sm"
          value={category}
          onChange={e => setCategory(e.target.value)}
        >
          {CATEGORIES.map(c => <option key={c} value={c}>{c === "all" ? "All categories" : c}</option>)}
        </select>

        {/* Level toggles */}
        <span className="text-sm text-muted">Levels:</span>
        {ALL_LEVELS.map(lv => (
          <button
            key={lv}
            className={`btn btn-sm ${levels.includes(lv) ? "btn-primary" : "btn-secondary"}`}
            style={{ color: levels.includes(lv) ? undefined : "var(--gray-500)" }}
            onClick={() => toggleLevel(lv)}
          >
            {lv}
          </button>
        ))}

        <div style={{ marginLeft: "auto", display: "flex", gap: ".5rem", alignItems: "center" }}>
          <span className="text-sm text-muted">{entries.length} events</span>
          <button
            className={`btn btn-sm ${paused ? "btn-primary" : "btn-secondary"}`}
            onClick={() => setPaused(p => !p)}
          >
            {paused ? "▶ Resume" : "⏸ Pause"}
          </button>
          <button
            className="btn btn-sm btn-danger"
            onClick={() => setShowClearModal(true)}
            disabled={clearing}
          >
            Clear logs
          </button>
        </div>
      </div>

      {/* Load more (older entries) */}
      {hasMore && (
        <div style={{ marginBottom: ".5rem" }}>
          <button className="btn btn-secondary btn-sm" onClick={() => load(false)} disabled={loading}>
            {loading ? "Loading…" : "Load older entries"}
          </button>
        </div>
      )}

      <div className="card event-feed">
        {entries.length === 0 ? (
          <div className="empty-state"><p>No events in this time window.</p></div>
        ) : (
          entries.map((e, i) => <EventEntry key={e.id ?? i} entry={e} />)
        )}
        <div ref={bottomRef} />
      </div>

      {showClearModal && (
        <ConfirmModal
          title="Clear all event logs?"
          message="This will permanently delete all event log entries from the database. This action cannot be undone."
          confirmLabel="Clear all logs"
          onConfirm={handleClear}
          onCancel={() => setShowClearModal(false)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Retention settings tab
// ---------------------------------------------------------------------------

function RetentionSettings() {
  const [form, setForm] = useState<LogRetentionSettings>({
    event_log_retention_days: 7,
    cycle_log_retention_days: 30,
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    getLogRetention().then(data => { setForm(data); setLoading(false); }).catch(() => setLoading(false));
  }, []);

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const updated = await setLogRetention(form);
      setForm(updated);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="loading"><div className="spinner" /> Loading settings…</div>;

  return (
    <div className="card" style={{ maxWidth: 560 }}>
      <div className="card-title" style={{ marginBottom: ".25rem" }}>Log Retention</div>
      <p className="text-sm text-muted" style={{ marginBottom: "1.5rem" }}>
        Configure how long log data is kept. The scheduler runs a purge daily and on each startup.
      </p>

      {error && <div className="badge badge-red" style={{ marginBottom: "1rem" }}>{error}</div>}

      <div className="form-group">
        <label className="form-label">Event log retention (days)</label>
        <input
          className="form-control"
          type="number"
          min="1"
          value={form.event_log_retention_days}
          onChange={e => setForm(f => ({ ...f, event_log_retention_days: Math.max(1, parseInt(e.target.value) || 1) }))}
        />
        <div className="form-hint">
          Event logs capture every engine action, vent movement, presence event, and state change.
          High volume — recommended 7 days. Older rows are deleted automatically.
        </div>
      </div>

      <div className="form-group">
        <label className="form-label">Cycle history retention (days)</label>
        <input
          className="form-control"
          type="number"
          min="1"
          value={form.cycle_log_retention_days}
          onChange={e => setForm(f => ({ ...f, cycle_log_retention_days: Math.max(1, parseInt(e.target.value) || 1) }))}
        />
        <div className="form-hint">
          Cycle history records one entry per HVAC cycle (start/stop, rooms, duration).
          Much lower volume than event logs — safe to keep for 30+ days.
        </div>
      </div>

      <div style={{ display: "flex", gap: ".75rem", alignItems: "center" }}>
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save"}
        </button>
        {saved && <span className="badge badge-green">Saved!</span>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Logs page (tabbed)
// ---------------------------------------------------------------------------

export default function Logs() {
  const [tab, setTab] = useState<"feed" | "history" | "retention">("feed");

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">Logs</div>
          <div className="page-subtitle">Cycle history, live event feed, and retention settings</div>
        </div>
        <div className="tab-bar">
          <button className={`tab-btn ${tab === "feed" ? "active" : ""}`} onClick={() => setTab("feed")}>
            Live Feed
          </button>
          <button className={`tab-btn ${tab === "history" ? "active" : ""}`} onClick={() => setTab("history")}>
            Cycle History
          </button>
          <button className={`tab-btn ${tab === "retention" ? "active" : ""}`} onClick={() => setTab("retention")}>
            Retention
          </button>
        </div>
      </div>

      {tab === "feed" && <LiveFeed />}
      {tab === "history" && <CycleHistory />}
      {tab === "retention" && <RetentionSettings />}
    </div>
  );
}
