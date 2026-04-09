import { useEffect, useState } from "react";
import { getLogs, type CycleLog } from "../api";

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

export default function Logs() {
  const [logs, setLogs] = useState<CycleLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [limit, setLimit] = useState(50);

  const load = async () => {
    const l = await getLogs(limit);
    setLogs(l);
    setLoading(false);
  };

  useEffect(() => { load(); }, [limit]);

  if (loading) return <div className="loading"><div className="spinner" /> Loading logs…</div>;

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">Cycle Logs</div>
          <div className="page-subtitle">{logs.length} cycles shown</div>
        </div>
        <div className="flex gap-sm">
          <select className="form-control form-control-sm" value={limit} onChange={e => setLimit(Number(e.target.value))}>
            <option value={20}>Last 20</option>
            <option value={50}>Last 50</option>
            <option value={100}>Last 100</option>
          </select>
          <button className="btn btn-secondary" onClick={load}>↻ Refresh</button>
        </div>
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
