import { useEffect, useRef, useState } from "react";
import { getDevLogs, getStatus, type EventLogEntry, type ZoneStatus } from "../api";
import { useDevMode } from "../main";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function actionIcon(details: Record<string, unknown> | null): string {
  const action = details?.action as string | undefined;
  if (action === "open_vent") return "💨↑";
  if (action === "close_vent") return "💨↓";
  if (action === "set_thermostat") return "🌡";
  return "⚙";
}

function actionColor(details: Record<string, unknown> | null): string {
  const action = details?.action as string | undefined;
  if (action === "open_vent") return "var(--green)";
  if (action === "close_vent") return "var(--blue)";
  if (action === "set_thermostat") return "var(--orange)";
  return "var(--gray-600)";
}

function fmtTime(ts: string): string {
  const d = new Date(ts);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fmtEntity(details: Record<string, unknown> | null): string {
  const eid = details?.entity_id as string | undefined;
  if (!eid) return "";
  // Return just the last part of the entity_id for brevity
  return eid.split(".")[1] ?? eid;
}

// ---------------------------------------------------------------------------
// Live action feed
// ---------------------------------------------------------------------------
function ActionFeed({ entries }: { entries: EventLogEntry[] }) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [entries.length]);

  if (entries.length === 0) {
    return (
      <div className="dev-feed-empty">
        <p>No dev actions logged yet.</p>
        <p style={{ marginTop: ".5rem", color: "var(--gray-400)" }}>
          The engine is running — actions will appear here as schedules/presence trigger cycle events.
        </p>
      </div>
    );
  }

  return (
    <div className="dev-feed">
      {entries.map(e => (
        <div key={e.id} className="dev-feed-row">
          <span className="dev-feed-time">{fmtTime(e.timestamp)}</span>
          <span className="dev-feed-icon" style={{ color: actionColor(e.details) }}>
            {actionIcon(e.details)}
          </span>
          <span className="dev-feed-msg">{e.message}</span>
          {e.details?.entity_id != null && (
            <span className="dev-feed-entity font-mono">{fmtEntity(e.details)}</span>
          )}
          {e.details?.temperature != null && (
            <span className="dev-feed-temp">{Number(e.details.temperature).toFixed(1)}°F</span>
          )}
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Zone status panel (same data as Dashboard but labeled for dev context)
// ---------------------------------------------------------------------------
function ZonePanel({ zones }: { zones: ZoneStatus[] }) {
  if (zones.length === 0) {
    return <p className="text-muted text-sm">No thermostat zones found.</p>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      {zones.map(zone => (
        <div key={zone.thermostat_entity_id} className="card">
          <div className="card-title">
            {zone.thermostat_entity_id}
            <span className="badge badge-gray" style={{ marginLeft: ".5rem", textTransform: "capitalize" }}>
              {zone.cycle_state}
            </span>
            <span className="badge badge-blue" style={{ marginLeft: ".35rem" }}>
              {zone.hvac_action || zone.hvac_mode || "—"}
            </span>
          </div>
          <div className="text-sm text-muted" style={{ marginBottom: ".75rem" }}>
            Current: <strong>{zone.current_temp != null ? `${zone.current_temp}°F` : "—"}</strong>
            {" · "}
            Setpoint: <strong>{zone.setpoint != null ? `${zone.setpoint}°F` : "—"}</strong>
          </div>
          {zone.rooms.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Room</th>
                    <th>Avg Temp</th>
                    <th>Presence</th>
                    <th>Vents</th>
                  </tr>
                </thead>
                <tbody>
                  {zone.rooms.map(r => (
                    <tr key={r.room_id}>
                      <td className="font-mono" style={{ fontSize: ".78rem" }}>{r.room_id.slice(0, 8)}</td>
                      <td>{r.avg_temp != null ? `${r.avg_temp}°F` : "—"}</td>
                      <td>{r.presence_active ? <span style={{ color: "var(--green)" }}>●</span> : <span style={{ color: "var(--gray-400)" }}>○</span>}</td>
                      <td>
                        {Object.entries(r.vent_states).map(([eid, state]) => (
                          <span key={eid} className="room-vent-pill" title={eid}
                            style={{ background: state === "open" ? "var(--green-light)" : "var(--gray-200)",
                                     color: state === "open" ? "#15803d" : "var(--gray-700)" }}>
                            {state}
                          </span>
                        ))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------
export default function DevModePage() {
  const { devMode, toggleDevMode } = useDevMode();
  const [entries, setEntries] = useState<EventLogEntry[]>([]);
  const [zones, setZones] = useState<ZoneStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [autoScroll, setAutoScroll] = useState(true);

  const fetchLogs = async () => {
    try {
      const logs = await getDevLogs(500);
      setEntries(logs.slice().reverse()); // oldest first for the feed
    } catch { /* ignore */ }
  };

  const fetchZones = async () => {
    try {
      const z = await getStatus();
      setZones(z);
    } catch { /* ignore */ }
  };

  useEffect(() => {
    Promise.all([fetchLogs(), fetchZones()]).finally(() => setLoading(false));
    const logsInterval = setInterval(fetchLogs, 3000);
    const zonesInterval = setInterval(fetchZones, 5000);
    return () => { clearInterval(logsInterval); clearInterval(zonesInterval); };
  }, []);

  if (!devMode) {
    return (
      <div>
        <div className="page-header">
          <div>
            <div className="page-title">Developer Mode</div>
            <div className="page-subtitle">Engine runs, but no changes are sent to Home Assistant</div>
          </div>
        </div>
        <div className="card" style={{ textAlign: "center", padding: "3rem 2rem" }}>
          <div style={{ fontSize: "3rem", marginBottom: "1rem" }}>🛠</div>
          <p style={{ fontWeight: 600, fontSize: "1.1rem", marginBottom: ".75rem" }}>Developer mode is off</p>
          <p className="text-muted" style={{ marginBottom: "1.5rem" }}>
            Enable it to run the cycle engine without making any real changes to thermostats or vents.
            All actions are logged here instead.
          </p>
          <button className="btn btn-primary" onClick={toggleDevMode}>Enable Developer Mode</button>
        </div>
      </div>
    );
  }

  if (loading) return <div className="loading"><div className="spinner" /> Loading…</div>;

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title" style={{ display: "flex", alignItems: "center", gap: ".75rem" }}>
            🛠 Developer Mode
            <span className="badge" style={{ background: "#fef3c7", color: "#92400e", fontSize: ".75rem" }}>
              ACTIVE — no HA changes
            </span>
          </div>
          <div className="page-subtitle">
            Engine is running. All thermostat and vent actions are intercepted and logged below.
          </div>
        </div>
        <button className="btn btn-secondary" onClick={toggleDevMode}>Disable Dev Mode</button>
      </div>

      <div className="dev-layout">
        {/* Left: action feed */}
        <div className="dev-feed-panel">
          <div className="dev-panel-header">
            <span style={{ fontWeight: 600 }}>Action Log</span>
            <span className="text-muted text-sm">auto-refreshes every 3s · {entries.length} entries</span>
            <button className="btn btn-secondary btn-sm" onClick={() => setEntries([])}>Clear</button>
          </div>
          <ActionFeed entries={entries} />
        </div>

        {/* Right: live zone status */}
        <div className="dev-zone-panel">
          <div className="dev-panel-header" style={{ marginBottom: "1rem" }}>
            <span style={{ fontWeight: 600 }}>Zone Status</span>
            <span className="text-muted text-sm">auto-refreshes every 5s</span>
          </div>
          <ZonePanel zones={zones} />
        </div>
      </div>
    </div>
  );
}
