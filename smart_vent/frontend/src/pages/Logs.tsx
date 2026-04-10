import { useEffect, useRef, useState } from "react";
import { getLogs, getEventLogs, connectWS, type CycleLog, type EventLogEntry } from "../api";

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
        <td>{new Date(log.started_at).toLocaleString()}</td>
        <td>{log.ended_at ? new Date(log.ended_at).toLocaleString() : <span className="badge badge-green">Active</span>}</td>
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

function CycleHistory() {
  const [logs, setLogs] = useState<CycleLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [limit, setLimit] = useState(50);

  const load = async () => {
    setLoading(true);
    const l = await getLogs(limit);
    setLogs(l);
    setLoading(false);
  };

  useEffect(() => { load(); }, [limit]);

  if (loading) return <div className="loading"><div className="spinner" /> Loading logs…</div>;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: ".5rem", marginBottom: "1rem" }}>
        <select className="form-control form-control-sm" value={limit} onChange={e => setLimit(Number(e.target.value))}>
          <option value={20}>Last 20</option>
          <option value={50}>Last 50</option>
          <option value={100}>Last 100</option>
        </select>
        <button className="btn btn-secondary" onClick={load}>↻ Refresh</button>
      </div>

      {logs.length === 0 ? (
        <div className="card"><div className="empty-state"><p>No cycle logs yet. Cycles will appear here once the system starts running.</p></div></div>
      ) : (
        <div className="card">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Thermostat</th>
                  <th>Mode</th>
                  <th>Started</th>
                  <th>Ended</th>
                  <th>Duration</th>
                  <th>Rooms</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {logs.map(l => <LogRow key={l.id} log={l} />)}
              </tbody>
            </table>
          </div>
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

const CATEGORIES = ["all", "system", "api", "engine", "presence", "ha"];

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
  const [paused, setPaused] = useState(false);
  const [loading, setLoading] = useState(true);
  const bottomRef = useRef<HTMLDivElement>(null);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  // Initial load
  useEffect(() => {
    getEventLogs(200, category === "all" ? undefined : category)
      .then(data => { setEntries(data.reverse()); setLoading(false); })
      .catch(() => setLoading(false));
  }, [category]);

  // WebSocket subscription for new events
  useEffect(() => {
    const cleanup = connectWS((event) => {
      if (event.type === "log_event" && !pausedRef.current) {
        const entry = event.data as unknown as EventLogEntry;
        if (category === "all" || entry.category === category) {
          setEntries(prev => [...prev.slice(-499), entry]);
        }
      }
    });
    return cleanup;
  }, [category]);

  // Auto-scroll to bottom when entries change (unless paused)
  useEffect(() => {
    if (!paused) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [entries, paused]);

  if (loading) return <div className="loading"><div className="spinner" /> Loading events…</div>;

  return (
    <div>
      <div style={{ display: "flex", gap: ".5rem", marginBottom: ".75rem", alignItems: "center" }}>
        <select
          className="form-control form-control-sm"
          value={category}
          onChange={e => setCategory(e.target.value)}
        >
          {CATEGORIES.map(c => <option key={c} value={c}>{c === "all" ? "All categories" : c}</option>)}
        </select>
        <button
          className={`btn ${paused ? "btn-primary" : "btn-secondary"}`}
          onClick={() => setPaused(p => !p)}
        >
          {paused ? "▶ Resume" : "⏸ Pause"}
        </button>
        <span className="text-sm text-muted">{entries.length} events</span>
      </div>

      <div className="card event-feed">
        {entries.length === 0 ? (
          <div className="empty-state"><p>No events yet. Events will appear here in real time.</p></div>
        ) : (
          entries.map((e, i) => <EventEntry key={e.id ?? i} entry={e} />)
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Logs page (tabbed)
// ---------------------------------------------------------------------------

export default function Logs() {
  const [tab, setTab] = useState<"history" | "feed">("feed");

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">Logs</div>
          <div className="page-subtitle">Cycle history and live event feed</div>
        </div>
        <div className="tab-bar">
          <button
            className={`tab-btn ${tab === "feed" ? "active" : ""}`}
            onClick={() => setTab("feed")}
          >Live Feed</button>
          <button
            className={`tab-btn ${tab === "history" ? "active" : ""}`}
            onClick={() => setTab("history")}
          >Cycle History</button>
        </div>
      </div>

      {tab === "history" ? <CycleHistory /> : <LiveFeed />}
    </div>
  );
}
