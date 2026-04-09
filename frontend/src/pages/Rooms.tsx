import { useEffect, useState } from "react";
import {
  getRooms, getRoom, createRoom, updateRoom, deleteRoom,
  addSensor, removeSensor, addVent, removeVent,
  addPresence, removePresence,
  type Room,
} from "../api";
import EntityPicker from "../components/EntityPicker";

// ---------------------------------------------------------------------------
// Room create / edit modal (name, thermostat, presence config)
// ---------------------------------------------------------------------------
function RoomModal({
  room,
  onClose,
  onSave,
}: {
  room: Room | null;
  onClose: () => void;
  onSave: (saved: Room) => void;
}) {
  const [name, setName] = useState(room?.name ?? "");
  const [thermostat, setThermostat] = useState(room?.thermostat_entity_id ?? "");
  const [sysTemp, setSysTemp] = useState(room?.system_wide_temp?.toString() ?? "");
  const [holdover, setHoldover] = useState(room?.presence_holdover_hours?.toString() ?? "2");
  const [includeThermoSensor, setIncludeThermoSensor] = useState(room?.include_thermostat_sensor ?? false);
  const [notes, setNotes] = useState(room?.notes ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const save = async () => {
    if (!name.trim()) { setError("Room name is required"); return; }
    if (!thermostat.trim()) { setError("Thermostat is required"); return; }
    setSaving(true);
    setError("");
    try {
      const payload = {
        name: name.trim(),
        thermostat_entity_id: thermostat.trim(),
        system_wide_temp: sysTemp ? parseFloat(sysTemp) : null,
        presence_holdover_hours: parseFloat(holdover) || 0,
        include_thermostat_sensor: includeThermoSensor,
        notes,
      };
      const saved = room ? await updateRoom(room.id, payload) : await createRoom(payload);
      onSave(saved);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={e => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-title">{room ? "Edit Room" : "New Room"}</div>
        {error && <div className="badge badge-red" style={{ marginBottom: "1rem" }}>{error}</div>}

        <div className="form-group">
          <label className="form-label">Room name *</label>
          <input className="form-control" value={name} onChange={e => setName(e.target.value)}
            placeholder="e.g. Master Bedroom" autoFocus />
        </div>

        <div className="form-group">
          <label className="form-label">Thermostat *</label>
          <EntityPicker
            domain="climate"
            placeholder="Search thermostats…"
            hasAttribute="hvac_action"
            excludeIcon="mdi:door-open"
            onSelect={setThermostat}
          />
          {thermostat && (
            <div className="tag" style={{ marginTop: ".4rem" }}>
              ✓ {thermostat}
              <button className="tag-remove" onClick={() => setThermostat("")}>×</button>
            </div>
          )}
        </div>

        <hr className="divider" />

        <div className="form-group">
          <label className="form-label">
            Presence-triggered temperature (°F)
            <span className="text-muted" style={{ fontWeight: 400, marginLeft: ".5rem" }}>
              — used when motion/presence detected, no active schedule
            </span>
          </label>
          <input className="form-control" type="number" step="0.5"
            value={sysTemp} onChange={e => setSysTemp(e.target.value)} placeholder="e.g. 72" />
        </div>

        <div className="form-group">
          <label className="form-label">
            Presence holdover (hours)
            <span className="text-muted" style={{ fontWeight: 400, marginLeft: ".5rem" }}>
              — keep room active this long after last motion; 0 = disabled
            </span>
          </label>
          <input className="form-control" type="number" step="0.5" min="0"
            value={holdover} onChange={e => setHoldover(e.target.value)} />
        </div>

        <div className="form-group">
          <label style={{ display: "flex", alignItems: "center", gap: ".5rem", cursor: "pointer" }}>
            <input type="checkbox" checked={includeThermoSensor}
              onChange={e => setIncludeThermoSensor(e.target.checked)} />
            <span>Include thermostat's built-in sensor in room temperature average</span>
          </label>
        </div>

        <div className="form-group">
          <label className="form-label">Notes</label>
          <textarea className="form-control" rows={2} value={notes}
            onChange={e => setNotes(e.target.value)} />
        </div>

        <div className="modal-footer">
          <button className="btn btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? "Saving…" : room ? "Save changes" : "Create room"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Entity section inside the configure view
// ---------------------------------------------------------------------------
function EntitySection({
  title,
  description,
  icon,
  items,
  domain,
  pickerPlaceholder,
  emptyHint,
  pickerProps,
  onAdd,
  onRemove,
}: {
  title: string;
  description: string;
  icon: string;
  items: string[];
  domain: string;
  pickerPlaceholder: string;
  emptyHint: string;
  pickerProps?: { hasAttribute?: string; excludeIcon?: string };
  onAdd: (id: string) => Promise<void>;
  onRemove: (id: string) => Promise<void>;
}) {
  return (
    <div style={{ marginBottom: "1.5rem" }}>
      <div style={{ display: "flex", alignItems: "center", gap: ".5rem", marginBottom: ".3rem" }}>
        <span style={{ fontSize: "1.2rem" }}>{icon}</span>
        <span style={{ fontWeight: 700, fontSize: "1rem" }}>{title}</span>
        {items.length > 0 && (
          <span className="badge badge-blue">{items.length}</span>
        )}
      </div>
      <p className="text-sm text-muted" style={{ marginBottom: ".75rem" }}>{description}</p>

      <EntityPicker
        domain={domain}
        placeholder={pickerPlaceholder}
        hasAttribute={pickerProps?.hasAttribute}
        excludeIcon={pickerProps?.excludeIcon}
        onSelect={onAdd}
      />

      {items.length === 0 ? (
        <p className="text-sm text-muted" style={{ marginTop: ".5rem", fontStyle: "italic" }}>
          {emptyHint}
        </p>
      ) : (
        <div className="tag-list">
          {items.map(id => (
            <span key={id} className="tag">
              <span className="font-mono">{id}</span>
              <button className="tag-remove" title="Remove" onClick={() => onRemove(id)}>×</button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Room configure view (sensors, vents, presence)
// ---------------------------------------------------------------------------
function RoomConfigure({
  room,
  onBack,
  onRoomUpdated,
}: {
  room: Room;
  onBack: () => void;
  onRoomUpdated: (r: Room) => void;
}) {
  const [editOpen, setEditOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const sensors = room.sensors?.map(s => s.entity_id) ?? [];
  const vents   = room.vents?.map(v => v.entity_id) ?? [];
  const presence = room.presence_sensors?.map(p => p.entity_id) ?? [];

  const refresh = async () => {
    const updated = await getRoom(room.id);
    onRoomUpdated(updated);
  };

  const wrap = (label: string, fn: (id: string) => Promise<unknown>) => async (id: string) => {
    setBusy(label);
    try { await fn(id); await refresh(); }
    finally { setBusy(null); }
  };

  return (
    <div>
      {/* Header */}
      <div style={{ marginBottom: "1.25rem" }}>
        <button className="btn btn-secondary btn-sm" onClick={onBack} style={{ marginBottom: ".75rem" }}>
          ← All rooms
        </button>
        <div className="flex-between">
          <div>
            <div className="page-title">{room.name}</div>
            <div className="font-mono text-muted" style={{ marginTop: ".2rem" }}>
              {room.thermostat_entity_id}
            </div>
          </div>
          <button className="btn btn-secondary btn-sm" onClick={() => setEditOpen(true)}>
            Edit settings
          </button>
        </div>
      </div>

      {/* Quick status strip */}
      <div className="card" style={{ marginBottom: "1.25rem", padding: ".875rem 1.25rem" }}>
        <div className="flex gap-md" style={{ flexWrap: "wrap" }}>
          <span className="text-sm">
            <strong>{sensors.length}</strong> <span className="text-muted">temp sensor{sensors.length !== 1 ? "s" : ""}</span>
          </span>
          <span className="text-muted">·</span>
          <span className="text-sm">
            <strong>{vents.length}</strong> <span className="text-muted">vent{vents.length !== 1 ? "s" : ""}</span>
          </span>
          <span className="text-muted">·</span>
          <span className="text-sm">
            <strong>{presence.length}</strong> <span className="text-muted">presence sensor{presence.length !== 1 ? "s" : ""}</span>
          </span>
          {room.system_wide_temp != null && (
            <>
              <span className="text-muted">·</span>
              <span className="text-sm text-muted">Presence temp: <strong>{room.system_wide_temp}°F</strong></span>
            </>
          )}
        </div>
      </div>

      {/* Warnings */}
      {sensors.length === 0 && (
        <div className="card" style={{ marginBottom: "1rem", borderColor: "#f59e0b", background: "#fffbeb" }}>
          <p className="text-sm" style={{ color: "#92400e" }}>
            ⚠ No temperature sensors — this room will be skipped during HVAC cycles.
          </p>
        </div>
      )}
      {vents.length === 0 && (
        <div className="card" style={{ marginBottom: "1rem", borderColor: "var(--gray-200)", background: "var(--gray-50)" }}>
          <p className="text-sm text-muted">
            ℹ No vents configured — this room will be monitor-only (temperatures tracked but no vent control).
          </p>
        </div>
      )}

      {busy && (
        <div className="loading" style={{ padding: ".5rem 0", marginBottom: ".5rem" }}>
          <div className="spinner" /> {busy}
        </div>
      )}

      {/* Entity sections */}
      <div className="card">
        <EntitySection
          title="Temperature Sensors"
          description="Used to calculate the room's average temperature. Add all sensors in this room. The thermostat's own sensor can optionally be included via Edit settings."
          icon="🌡"
          items={sensors}
          domain="sensor"
          pickerPlaceholder="Search temperature sensors (sensor.*)…"
          emptyHint="No sensors added yet — search above to add one."
          onAdd={wrap("Adding sensor…", id => addSensor(room.id, id))}
          onRemove={wrap("Removing sensor…", id => removeSensor(room.id, id))}
        />

        <hr className="divider" />

        <EntitySection
          title="Flair Vents"
          description="Vents in this room controlled as cover entities. When the room hits its target temperature the system closes these vents."
          icon="💨"
          items={vents}
          domain="cover"
          pickerPlaceholder="Search Flair vents (cover.*)…"
          emptyHint="No vents added yet — search above to add one. Rooms without vents are monitor-only."
          onAdd={wrap("Adding vent…", (id: string) => addVent(room.id, id))}
          onRemove={wrap("Removing vent…", (id: string) => removeVent(room.id, id))}
        />

        <hr className="divider" />

        <EntitySection
          title="Presence / Motion Sensors"
          description="When any sensor here detects motion, the room activates at the presence-triggered temperature and stays active for the configured holdover period (reset on each detection)."
          icon="🚶"
          items={presence}
          domain="binary_sensor"
          pickerPlaceholder="Search motion/presence sensors (binary_sensor.*)…"
          emptyHint="No presence sensors added — the room will only activate via schedules."
          onAdd={wrap("Adding sensor…", (id: string) => addPresence(room.id, id))}
          onRemove={wrap("Removing sensor…", (id: string) => removePresence(room.id, id))}
        />
      </div>

      {editOpen && (
        <RoomModal
          room={room}
          onClose={() => setEditOpen(false)}
          onSave={async () => { setEditOpen(false); await refresh(); }}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Room list card
// ---------------------------------------------------------------------------
function RoomCard({
  room,
  onConfigure,
  onEdit,
  onDelete,
}: {
  room: Room;
  onConfigure: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const sensors  = room.sensors?.length ?? 0;
  const vents    = room.vents?.length ?? 0;
  const presence = room.presence_sensors?.length ?? 0;

  const missing = sensors === 0 || vents === 0;

  return (
    <div className="card">
      <div className="flex-between" style={{ marginBottom: ".5rem" }}>
        <div className="card-title" style={{ marginBottom: 0 }}>{room.name}</div>
        <button
          className="btn btn-danger btn-sm"
          onClick={onDelete}
        >
          Delete
        </button>
      </div>

      <div className="font-mono text-muted" style={{ marginBottom: ".875rem", fontSize: ".78rem" }}>
        {room.thermostat_entity_id}
      </div>

      {/* Entity counts */}
      <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap", marginBottom: ".875rem" }}>
        <span className={`badge ${sensors > 0 ? "badge-green" : "badge-red"}`}>
          🌡 {sensors} sensor{sensors !== 1 ? "s" : ""}
        </span>
        <span className={`badge ${vents > 0 ? "badge-blue" : "badge-gray"}`}>
          💨 {vents} vent{vents !== 1 ? "s" : ""}
        </span>
        <span className={`badge ${presence > 0 ? "badge-green" : "badge-gray"}`}>
          🚶 {presence} presence
        </span>
      </div>

      {missing && (
        <p className="text-sm" style={{ color: "#b45309", marginBottom: ".75rem" }}>
          ⚠ {sensors === 0 ? "No temperature sensors" : "No vents"} — configure below.
        </p>
      )}

      <div className="flex gap-sm">
        <button className="btn btn-primary btn-sm" style={{ flex: 1 }} onClick={onConfigure}>
          Configure sensors &amp; vents →
        </button>
        <button className="btn btn-secondary btn-sm" onClick={onEdit}>
          Settings
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------
export default function Rooms() {
  const [rooms, setRooms] = useState<Room[]>([]);
  const [loading, setLoading] = useState(true);
  const [showModal, setShowModal] = useState(false);
  const [editRoom, setEditRoom] = useState<Room | null>(null);
  const [configRoom, setConfigRoom] = useState<Room | null>(null);

  const load = async () => {
    // Fetch full room details (includes sensor/vent/presence arrays) for all rooms
    const list = await getRooms();
    const detailed = await Promise.all(list.map(r => getRoom(r.id)));
    setRooms(detailed);
    setLoading(false);
  };

  useEffect(() => { load(); }, []);

  if (loading) return <div className="loading"><div className="spinner" /> Loading rooms…</div>;

  // Configure view
  if (configRoom) {
    return (
      <RoomConfigure
        room={configRoom}
        onBack={() => { setConfigRoom(null); load(); }}
        onRoomUpdated={updated => setConfigRoom(updated)}
      />
    );
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">Rooms</div>
          <div className="page-subtitle">
            {rooms.length} room{rooms.length !== 1 ? "s" : ""}
            {rooms.length > 0 && ` · click "Configure sensors & vents" to set up a room`}
          </div>
        </div>
        <button className="btn btn-primary" onClick={() => { setEditRoom(null); setShowModal(true); }}>
          + Add room
        </button>
      </div>

      {rooms.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <p>No rooms yet.</p>
            <p style={{ marginTop: ".5rem" }}>
              Click <strong>+ Add room</strong>, pick a thermostat, then configure its sensors and vents.
            </p>
          </div>
        </div>
      ) : (
        <div className="card-grid">
          {rooms.map(room => (
            <RoomCard
              key={room.id}
              room={room}
              onConfigure={() => setConfigRoom(room)}
              onEdit={() => { setEditRoom(room); setShowModal(true); }}
              onDelete={async () => {
                if (confirm(`Delete room "${room.name}"?`)) {
                  await deleteRoom(room.id);
                  load();
                }
              }}
            />
          ))}
        </div>
      )}

      {showModal && (
        <RoomModal
          room={editRoom}
          onClose={() => setShowModal(false)}
          onSave={async saved => {
            setShowModal(false);
            await load();
            // If creating new room, immediately go to configure view
            if (!editRoom) {
              const full = await getRoom(saved.id);
              setConfigRoom(full);
            }
          }}
        />
      )}
    </div>
  );
}
