import { useEffect, useRef, useState } from "react";
import {
  getDevLogs,
  getStatus,
  seedDemoMetrics,
  type EventLogEntry,
  type ZoneStatus,
} from "../api";
import { useDevMode, useUnit } from "../contexts";
import { Frozen } from "../ci";

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
  // Append "Z" so the browser treats the bare UTC ISO string as UTC and
  // converts it to the user's local timezone (without "Z" browsers interpret
  // it as local time, displaying UTC hours as if they were already local).
  const d = new Date(ts + "Z");
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
  const { fmtTemp } = useUnit();
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [entries.length]);

  if (entries.length === 0) {
    return (
      <div className="dev-feed-empty">
        <p>No dev actions logged yet.</p>
        <p style={{ marginTop: ".5rem", color: "var(--gray-400)" }}>
          The engine is running — actions will appear here as schedules/presence trigger cycle
          events.
        </p>
      </div>
    );
  }

  return (
    <div className="dev-feed">
      {entries.map((e) => (
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
            <span className="dev-feed-temp">{fmtTemp(Number(e.details.temperature))}</span>
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
  const { fmtTemp } = useUnit();
  if (zones.length === 0) {
    return <p className="text-muted text-sm">No thermostat zones found.</p>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      {zones.map((zone) => (
        <div key={zone.thermostat_entity_id} className="card">
          <div className="card-title">
            {zone.thermostat_entity_id}
            <span
              className="badge badge-gray"
              style={{ marginLeft: ".5rem", textTransform: "capitalize" }}
            >
              {/* cycle_state advances as the engine runs cycles */}
              <Frozen>{zone.cycle_state}</Frozen>
            </span>
            <span className="badge badge-blue" style={{ marginLeft: ".35rem" }}>
              <Frozen>{zone.hvac_action || zone.hvac_mode || "—"}</Frozen>
            </span>
          </div>
          <div className="text-sm text-muted" style={{ marginBottom: ".75rem" }}>
            Current: <strong>{zone.current_temp != null ? fmtTemp(zone.current_temp) : "—"}</strong>
            {" · "}
            Setpoint: <strong>{zone.setpoint != null ? fmtTemp(zone.setpoint) : "—"}</strong>
          </div>
          {zone.rooms.length > 0 && (
            <div className="table-wrap">
              <table className="table-cards">
                <thead>
                  <tr>
                    <th>Room</th>
                    <th>Avg Temp</th>
                    <th>Presence</th>
                    <th>Vents</th>
                  </tr>
                </thead>
                <tbody>
                  {zone.rooms.map((r) => (
                    <tr key={r.room_id}>
                      <td data-label="Room" className="font-mono" style={{ fontSize: ".78rem" }}>
                        {/* Room ids are random uuids, freshly minted each time the
                            E2E stack recreates rooms, so this live zone-status row
                            would churn the devmode golden every run. Freeze it under
                            CI (renders "—"); the real id still shows in production. */}
                        <Frozen>{r.room_id.slice(0, 8)}</Frozen>
                      </td>
                      <td data-label="Avg Temp">
                        {r.avg_temp != null ? fmtTemp(r.avg_temp) : "—"}
                      </td>
                      <td data-label="Presence">
                        {r.presence_active ? (
                          <span style={{ color: "var(--green)" }}>●</span>
                        ) : (
                          <span style={{ color: "var(--gray-400)" }}>○</span>
                        )}
                      </td>
                      <td data-label="Vents">
                        {Object.entries(r.vent_states).map(([eid, state]) => (
                          <span
                            key={eid}
                            className="room-vent-pill"
                            title={eid}
                            style={{
                              background:
                                state === "open" ? "var(--green-light)" : "var(--gray-200)",
                              color: state === "open" ? "var(--green-text)" : "var(--gray-700)",
                            }}
                          >
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
  const [seedResult, setSeedResult] = useState("");
  // "Cleared" watermark: rows with id <= this are hidden so the 3s poll can't
  // repopulate a feed the user cleared. Genuinely new events have higher ids
  // and still appear. (Issue #303)
  const clearedBeforeIdRef = useRef(0);

  const fetchLogs = async () => {
    try {
      const logs = await getDevLogs(500);
      const visible = logs.filter((l) => l.id > clearedBeforeIdRef.current);
      setEntries(visible.slice().reverse()); // oldest first for the feed
    } catch {
      /* ignore */
    }
  };

  const clearFeed = () => {
    // Durable clear: record the newest id currently shown as a watermark, then
    // empty the feed. Subsequent polls filter everything at or below it.
    clearedBeforeIdRef.current = entries.reduce(
      (max, e) => Math.max(max, e.id),
      clearedBeforeIdRef.current
    );
    setEntries([]);
  };

  const fetchZones = async () => {
    try {
      const z = await getStatus();
      setZones(z);
    } catch {
      /* ignore */
    }
  };

  const seedDemo = async () => {
    setSeedResult("Seeding…");
    try {
      const r = await seedDemoMetrics();
      setSeedResult(
        `Seeded ${r.seeded_cycles} cycles (${r.eco_cycles} Eco-relaxed) and ` +
          `${r.seeded_events} feed events over ${r.start_date} → ${r.end_date}`
      );
    } catch (e) {
      setSeedResult(e instanceof Error ? e.message : "Seeding failed");
    }
  };

  useEffect(() => {
    // No point polling dev logs / zone status when developer mode is off —
    // the OFF view doesn't render them. Skipping also avoids state updates
    // landing after the component is torn down.
    if (!devMode) {
      setLoading(false);
      return;
    }
    Promise.all([fetchLogs(), fetchZones()]).finally(() => setLoading(false));
    const logsInterval = setInterval(fetchLogs, 3000);
    const zonesInterval = setInterval(fetchZones, 5000);
    return () => {
      clearInterval(logsInterval);
      clearInterval(zonesInterval);
    };
  }, [devMode]);

  if (!devMode) {
    return (
      <div>
        <div className="page-header">
          <div>
            <div className="page-title">Developer Mode</div>
            <div className="page-subtitle">
              Engine runs, but no changes are sent to Home Assistant
            </div>
          </div>
        </div>
        <div className="card" style={{ textAlign: "center", padding: "3rem 2rem" }}>
          <div style={{ fontSize: "3rem", marginBottom: "1rem" }}>🛠</div>
          <p style={{ fontWeight: 600, fontSize: "1.1rem", marginBottom: ".75rem" }}>
            Developer mode is off
          </p>
          <p className="text-muted" style={{ marginBottom: "1.5rem" }}>
            Enable it to run the cycle engine without making any real changes to thermostats or
            vents. All actions are logged here instead.
          </p>
          <button className="btn btn-primary" onClick={toggleDevMode}>
            Enable Developer Mode
          </button>
        </div>
      </div>
    );
  }

  if (loading)
    return (
      <div className="loading">
        <div className="spinner" /> Loading…
      </div>
    );

  return (
    <div>
      <div className="page-header">
        <div>
          <div
            className="page-title"
            style={{ display: "flex", alignItems: "center", gap: ".75rem" }}
          >
            🛠 Developer Mode
            <span
              className="badge"
              style={{
                background: "var(--orange-light)",
                color: "var(--orange-text)",
                fontSize: ".75rem",
              }}
            >
              ACTIVE — no HA changes
            </span>
          </div>
          <div className="page-subtitle">
            Engine is running. All thermostat and vent actions are intercepted and logged below.
          </div>
        </div>
        <button className="btn btn-secondary" onClick={toggleDevMode}>
          Disable Dev Mode
        </button>
      </div>

      {/* Demo metrics seeding (Issue #442) — deterministic fixture week for
          the Metrics page. Reseeding replaces only the demo rows. */}
      <div className="card" style={{ marginBottom: "1rem" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "1rem", flexWrap: "wrap" }}>
          <button className="btn btn-secondary" onClick={seedDemo}>
            Seed demo metrics
          </button>
          <span className="text-muted text-sm">
            Writes a deterministic week of demo cycles (2025-06-01 → 2025-06-07) so the Metrics page
            charts render without waiting days for real data. Real cycle history is never touched;
            reseeding replaces the demo rows.
          </span>
        </div>
        {seedResult && (
          <div className="text-sm" style={{ marginTop: ".5rem" }}>
            {seedResult}
          </div>
        )}
      </div>

      <div className="dev-layout">
        {/* Left: action feed */}
        <div className="dev-feed-panel">
          <div className="dev-panel-header">
            <span style={{ fontWeight: 600 }}>Action Log</span>
            <span className="text-muted text-sm">
              auto-refreshes every 3s · <Frozen>{entries.length}</Frozen> entries
            </span>
            <button className="btn btn-secondary btn-sm" onClick={clearFeed}>
              Clear
            </button>
          </div>
          {/* The action feed grows as the engine logs cycle events between the
              two screenshot passes — show the deterministic empty state under CI. */}
          <Frozen frozen={<ActionFeed entries={[]} />}>
            <ActionFeed entries={entries} />
          </Frozen>
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
