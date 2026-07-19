import { useEffect, useState } from "react";
import {
  getStatus,
  getRooms,
  getThermostats,
  getVacationMode,
  clearPresenceHoldover,
  connectWS,
  type ZoneStatus,
  type Room,
  type ThermostatConfig,
  type VacationMode,
} from "../api";
import { useUnit } from "../contexts";
import { Frozen, FROZEN } from "../ci";
import AirflowConfigBanner from "../components/AirflowConfigBanner";
import StaleSensorsBanner from "../components/StaleSensorsBanner";
import UnavailableThermostatsBanner from "../components/UnavailableThermostatsBanner";
import VacationModeModal from "../components/VacationModeModal";
import EcoSuspendModal from "../components/EcoSuspendModal";

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

function RoomRow({
  r,
  rooms,
  onClearPresence,
}: {
  r: ZoneStatus["rooms"][number];
  rooms: Room[];
  onClearPresence: () => void;
}) {
  const { fmtTemp } = useUnit();
  const room = rooms.find((x) => x.id === r.room_id);
  const ventEntries = Object.entries(r.vent_states);
  const openCount = ventEntries.filter(([, s]) => s === "open").length;
  const [clearing, setClearing] = useState(false);

  const clearPresence = async () => {
    setClearing(true);
    try {
      await clearPresenceHoldover(r.room_id);
      onClearPresence();
    } finally {
      setClearing(false);
    }
  };

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
        <span style={{ textAlign: "right" }}>
          <span className="stat-value">{r.avg_temp != null ? fmtTemp(r.avg_temp) : "—"}</span>
          {r.target_temp != null &&
            (r.eco_active && r.requested_target != null ? (
              // Eco Mode is relaxing this room (Issue #404): show the requested
              // ask and the relaxed effective target it is actually running to.
              // The relaxed target can be fractional — it is the room's true
              // stop condition; only the setpoint commanded to the thermostat
              // is rounded to a whole degree. A target equal to the requested
              // value (possible in state persisted by older versions that
              // rounded the target itself) renders without the redundant
              // "requested" echo.
              // Rendered live (not frozen) — the values are stable for a given
              // fixture, so the golden covers the indicator content.
              <span
                className="text-sm text-muted"
                style={{ display: "block", fontWeight: 500 }}
                title="Eco Mode relaxed this room's target based on the outdoor temperature; only the setpoint sent to the thermostat is rounded to a whole degree"
              >
                {r.target_temp === r.requested_target ? (
                  <>🌿 {fmtTemp(r.target_temp)} — Eco</>
                ) : (
                  <>
                    🌿 {fmtTemp(r.target_temp)} · requested {fmtTemp(r.requested_target)} — Eco
                  </>
                )}
              </span>
            ) : (
              <span
                className="text-sm text-muted"
                style={{ display: "block", fontWeight: 500 }}
                title="Temperature this room is requesting from the cycle"
              >
                🎯 requesting {fmtTemp(r.target_temp)}
              </span>
            ))}
        </span>
      </div>
      {r.presence_active && (
        <button
          className="btn btn-sm btn-outline-danger"
          style={{ padding: "0 .5rem", fontSize: ".75rem" }}
          onClick={clearPresence}
          disabled={clearing}
        >
          {clearing ? "Clearing…" : "Clear presence"}
        </button>
      )}
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

// Renders the "Active rooms" list for a zone. Factored out so the live path and
// the CI-frozen placeholder below share exactly one markup definition.
function activeRoomsBlock(
  roomList: ZoneStatus["rooms"],
  rooms: Room[],
  onClearPresence: () => void
) {
  if (roomList.length === 0) return null;
  return (
    <div style={{ marginTop: "1rem" }}>
      <div
        className="text-sm"
        style={{ fontWeight: 600, marginBottom: ".5rem", color: "var(--gray-700)" }}
      >
        Active rooms
      </div>
      {roomList.map((r) => (
        <RoomRow key={r.room_id} r={r} rooms={rooms} onClearPresence={onClearPresence} />
      ))}
    </div>
  );
}

// The live active-rooms list is engine-driven (membership, counts and vent
// positions flip between the update and verify screenshot passes), so it is
// frozen out of the golden under CI. That left the requesting-temp line and the
// Clear-presence button — both of which only appear on an active room — with no
// visual-regression coverage. Under CI we therefore render these fixed,
// representative rows in its place: deterministic (no engine/wall-clock input),
// unit-aware via fmtTemp, and mirroring the real-world case (Bedroom reading
// 71.4° while requesting 68°). Two rooms are shown so the golden makes it clear
// the requesting line and Clear-presence button render once *per room* — not
// once for the whole section. Shown on a single zone card only (see
// showActiveRoomsSample) to avoid a duplicate.
const CI_SAMPLE_ACTIVE_ROOMS: ZoneStatus["rooms"] = [
  {
    room_id: "Bedroom",
    avg_temp: 71.4,
    target_temp: 68,
    presence_active: true,
    vent_states: { "cover.bedroom_vent": "open" },
  },
  {
    room_id: "Office",
    avg_temp: 73.0,
    target_temp: 70,
    presence_active: true,
    vent_states: { "cover.office_vent": "closed" },
  },
];

function ZoneCard({
  zone,
  rooms,
  thermostats,
  onClearPresence,
  onSuspendEco,
  showActiveRoomsSample,
}: {
  zone: ZoneStatus;
  rooms: Room[];
  thermostats: ThermostatConfig[];
  onClearPresence: () => void;
  onSuspendEco: (thermostatEntityId: string) => void;
  showActiveRoomsSample: boolean;
}) {
  const { fmtTemp } = useUnit();
  const colorClass = modeColor(zone.hvac_action);
  const label = modeLabel(zone.hvac_action, zone.hvac_mode);
  const badgeClass = `badge badge-${colorClass}`;
  const tc = thermostats.find((t) => t.thermostat_entity_id === zone.thermostat_entity_id);
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
        {/* hvac action badge flips Cooling/Heating/Idle as the engine cycles */}
        <Frozen frozen={<span className="badge badge-gray">{FROZEN}</span>}>
          <span className={badgeClass}>{label}</span>
        </Frozen>
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
        {/* cycle_state (running/idle) and its colour change per engine cycle */}
        <Frozen frozen={<span className="badge badge-gray">{FROZEN}</span>}>
          <span className={`badge badge-${zone.cycle_state === "running" ? "blue" : "gray"}`}>
            {zone.cycle_state}
          </span>
        </Frozen>
      </div>
      <div className="stat-row">
        <span className="stat-label">Active rooms</span>
        <span className="stat-value">
          {/* active count tracks which rooms the engine is cycling right now */}
          <Frozen>{activeRooms}</Frozen> / {totalRooms}
        </span>
      </div>

      {/* Progress bar + active-rooms list reflect live cycle membership, which
          flips between the update and verify screenshot passes. Drop them under
          CI so the card is deterministic. */}
      <Frozen frozen={null}>
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
      </Frozen>

      <Frozen
        frozen={
          showActiveRoomsSample
            ? activeRoomsBlock(CI_SAMPLE_ACTIVE_ROOMS, rooms, onClearPresence)
            : null
        }
      >
        {activeRoomsBlock(zone.rooms, rooms, onClearPresence)}
      </Frozen>

      {/* Eco Suspend (Issue #500): per-zone control, shown when Eco is in play
          for this thermostat (enabled, or already suspended). Static config
          data — no <Frozen> needed. */}
      {tc && (tc.eco_mode_enabled || tc.eco_suspend_until) && (
        <div style={{ marginTop: ".75rem" }}>
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => onSuspendEco(tc.thermostat_entity_id)}
            style={{ width: "100%" }}
          >
            🍃{" "}
            {tc.eco_suspend_until
              ? `Eco suspended until ${new Date(tc.eco_suspend_until).toLocaleString()} — manage`
              : "Suspend Eco"}
          </button>
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
  const [vacationMode, setVacationMode] = useState<VacationMode>({
    enabled: false,
    return_at: null,
  });
  const [showVacationModal, setShowVacationModal] = useState(false);
  // Eco Suspend modal (#500): null = closed; { thermostat } = open, optionally
  // pre-scoped to the zone card it was opened from.
  const [ecoSuspendFor, setEcoSuspendFor] = useState<{ thermostat?: string } | null>(null);

  const load = async () => {
    const [z, r, tc, vm] = await Promise.all([
      getStatus(),
      getRooms(),
      getThermostats(),
      getVacationMode(),
    ]);
    setZones(z);
    setRooms(r);
    setThermostats(tc);
    setVacationMode(vm ?? { enabled: false, return_at: null });
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
            {zones.length} zone{zones.length !== 1 ? "s" : ""} · {rooms.length} room
            {rooms.length !== 1 ? "s" : ""}
            {lastUpdate && (
              <>
                {" · Updated "}
                <Frozen>{lastUpdate.toLocaleTimeString()}</Frozen>
              </>
            )}
          </div>
        </div>
        <button className="btn btn-secondary" onClick={load}>
          ↻ Refresh
        </button>
      </div>

      <AirflowConfigBanner />
      <UnavailableThermostatsBanner />
      <StaleSensorsBanner />

      {/* Vacation mode + Eco Suspend buttons — sit above climate cards */}
      <div style={{ marginBottom: "1rem" }}>
        <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap" }}>
          <button
            className={`btn ${vacationMode.enabled ? "btn-warning" : "btn-secondary"}`}
            onClick={() => setShowVacationModal(true)}
            style={{ display: "flex", alignItems: "center", gap: ".5rem" }}
          >
            ✈ {vacationMode.enabled ? "Vacation mode active" : "Enable vacation mode"}
          </button>
          {thermostats.length > 0 && (
            <button
              className="btn btn-secondary"
              data-testid="dashboard-eco-suspend-btn"
              onClick={() => setEcoSuspendFor({})}
              style={{ display: "flex", alignItems: "center", gap: ".5rem" }}
            >
              🍃{" "}
              {thermostats.some((t) => t.eco_suspend_until)
                ? "Eco suspended — manage"
                : "Suspend Eco"}
            </button>
          )}
        </div>
        {vacationMode.enabled && vacationMode.return_at && (
          <div className="form-hint" style={{ marginTop: ".4rem" }}>
            Schedules paused until {new Date(vacationMode.return_at).toLocaleString()}
          </div>
        )}
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
          {zones.map((z, i) => (
            <ZoneCard
              key={z.thermostat_entity_id}
              zone={z}
              rooms={rooms}
              thermostats={thermostats}
              onClearPresence={load}
              onSuspendEco={(tid) => setEcoSuspendFor({ thermostat: tid })}
              // Render the CI active-rooms sample on the first zone card only.
              showActiveRoomsSample={i === 0}
            />
          ))}
        </div>
      )}

      {showVacationModal && (
        <VacationModeModal
          current={vacationMode}
          onClose={() => setShowVacationModal(false)}
          onChanged={(updated) => {
            setVacationMode(updated);
            setShowVacationModal(false);
          }}
        />
      )}

      {ecoSuspendFor && (
        <EcoSuspendModal
          thermostats={thermostats}
          initialThermostat={ecoSuspendFor.thermostat}
          onClose={() => setEcoSuspendFor(null)}
          onChanged={load}
        />
      )}
    </div>
  );
}
