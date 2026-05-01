import { useEffect, useState } from "react";
import {
  getStatus,
  getRooms,
  getThermostats,
  connectWS,
  type ZoneStatus,
  type Room,
  type ThermostatConfig,
} from "../api";
import { useUnit } from "../contexts";

function modeColor(mode: string): string {
  if (mode === "cooling") return "blue";
  if (mode === "heating") return "orange";
  return "gray";
}

function modeLabel(action: string, state: string): string {
  if (action === "cooling") return "Cooling";
  if (action === "heating") return "Heating";
  if (state === "idle") return "Idle";
  if (state === "off") return "Off";
  return state;
}

function RoomRow({ r, rooms }: { r: ZoneStatus["rooms"][number]; rooms: Room[] }) {
  const { fmtTemp } = useUnit();
  const room = rooms.find((x) => x.id === r.room_id);
  const ventEntries = Object.entries(r.vent_states);
  const openCount = ventEntries.filter(([, s]) => s === "open").length;

  return (
    <div
      className="stat-row"
      style={{ flexDirection: "column", alignItems: "flex-start", gap: ".4rem" }}
    >
      <div className="flex-between" style={{ width: "100%" }}>
        <span className="stat-label" style={{ fontWeight: 600, color: "var(--gray-900)" }}>
          {room?.name ?? r.room_id}
          {r.presence_active && (
            <span className="badge badge-green" style={{ marginLeft: ".5rem" }}>
              Presence
            </span>
          )}
        </span>
        <span className="stat-value">{r.avg_temp != null ? fmtTemp(r.avg_temp) : "—"}</span>
      </div>
      {ventEntries.length > 0 && (
        <div className="flex gap-sm" style={{ flexWrap: "wrap" }}>
          {ventEntries.map(([eid, state]) => (
            <span key={eid} className="tag">
              <span className={`vent-dot ${state}`} />
              {eid.split(".").pop()} · {state}
            </span>
          ))}
          <span className="text-sm text-muted">
            {openCount}/{ventEntries.length} open
          </span>
        </div>
      )}
    </div>
  );
}

function ZoneCard({
  zone,
  rooms,
  thermostats,
}: {
  zone: ZoneStatus;
  rooms: Room[];
  thermostats: ThermostatConfig[];
}) {
  const { fmtTemp } = useUnit();
  const colorClass = modeColor(zone.hvac_action);
  const label = modeLabel(zone.hvac_action, zone.hvac_mode);
  const badgeClass = `badge badge-${colorClass}`;
  const zoneRooms = rooms.filter((r) => r.thermostat_entity_id === zone.thermostat_entity_id);
  const totalRooms = zoneRooms.length;
  const activeRooms = zone.rooms.length;
  const doneRooms = zone.rooms.filter((r) =>
    Object.values(r.vent_states).every((s) => s === "closed")
  ).length;
  const progress = activeRooms > 0 ? doneRooms / activeRooms : 0;

  return (
    <div className="card">
      <div className="flex-between" style={{ marginBottom: ".75rem" }}>
        <div>
          <div className="card-title" style={{ marginBottom: 0 }}>
            {thermostats.find((t) => t.thermostat_entity_id === zone.thermostat_entity_id)?.name ||
              zone.thermostat_entity_id.split(".").pop()?.replace(/_/g, " ")}
          </div>
          <div className="card-subtitle font-mono" style={{ marginBottom: 0, fontSize: ".75rem" }}>
            {zone.thermostat_entity_id}
          </div>
        </div>
        <span className={badgeClass}>{label}</span>
      </div>

      <div className="stat-row">
        <span className="stat-label">Ambient</span>
        <span className="stat-value">
          {zone.current_temp != null ? fmtTemp(zone.current_temp) : "—"}
        </span>
      </div>
      <div className="stat-row">
        <span className="stat-label">Setpoint</span>
        <span className="stat-value">{zone.setpoint != null ? fmtTemp(zone.setpoint) : "—"}</span>
      </div>
      <div className="stat-row">
        <span className="stat-label">Cycle</span>
        <span className={`badge badge-${zone.cycle_state === "running" ? "blue" : "gray"}`}>
          {zone.cycle_state}
        </span>
      </div>
      <div className="stat-row">
        <span className="stat-label">Active rooms</span>
        <span className="stat-value">
          {activeRooms} / {totalRooms}
        </span>
      </div>

      {zone.cycle_state === "running" && activeRooms > 0 && (
        <>
          <div className="progress-bar" style={{ marginTop: ".75rem" }}>
            <div
              className={`progress-fill ${colorClass === "blue" ? "cooling" : "heating"}`}
              style={{ width: `${progress * 100}%` }}
            />
          </div>
          <div className="text-sm text-muted" style={{ marginTop: ".25rem" }}>
            {doneRooms}/{activeRooms} rooms at target
          </div>
        </>
      )}

      {zone.rooms.length > 0 && (
        <div style={{ marginTop: "1rem" }}>
          <div
            className="text-sm"
            style={{ fontWeight: 600, marginBottom: ".5rem", color: "var(--gray-700)" }}
          >
            Active rooms
          </div>
          {zone.rooms.map((r) => (
            <RoomRow key={r.room_id} r={r} rooms={rooms} />
          ))}
        </div>
      )}
    </div>
  );
}

export default function Dashboard() {
  const [zones, setZones] = useState<ZoneStatus[]>([]);
  const [rooms, setRooms] = useState<Room[]>([]);
  const [thermostats, setThermostats] = useState<ThermostatConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);

  const load = async () => {
    const [z, r, tc] = await Promise.all([getStatus(), getRooms(), getThermostats()]);
    setZones(z);
    setRooms(r);
    setThermostats(tc);
    setLastUpdate(new Date());
    setLoading(false);
  };

  useEffect(() => {
    load();
    const cancel = connectWS((event) => {
      if (event.type === "zone_status") {
        load();
      }
    });
    const interval = setInterval(load, 30000);
    return () => {
      cancel();
      clearInterval(interval);
    };
  }, []);

  if (loading)
    return (
      <div className="loading">
        <div className="spinner" /> Loading dashboard…
      </div>
    );

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">Dashboard</div>
          <div className="page-subtitle">
            {zones.length} zone{zones.length !== 1 ? "s" : ""} · {rooms.length} rooms
            {lastUpdate && ` · Updated ${lastUpdate.toLocaleTimeString()}`}
          </div>
        </div>
        <button className="btn btn-secondary" onClick={load}>
          ↻ Refresh
        </button>
      </div>

      {zones.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <p>No thermostat zones configured yet.</p>
            <p style={{ marginTop: ".5rem" }}>
              Go to <strong>Rooms</strong> to add your first room.
            </p>
          </div>
        </div>
      ) : (
        <div className="card-grid">
          {zones.map((z) => (
            <ZoneCard
              key={z.thermostat_entity_id}
              zone={z}
              rooms={rooms}
              thermostats={thermostats}
            />
          ))}
        </div>
      )}
    </div>
  );
}
