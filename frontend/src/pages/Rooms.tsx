import { useEffect, useState } from "react";
import {
  getRooms, createRoom, updateRoom, deleteRoom,
  addSensor, removeSensor, addVent, removeVent,
  addPresence, removePresence,
  type Room,
} from "../api";
import EntityPicker from "../components/EntityPicker";

function RoomModal({
  room,
  onClose,
  onSave,
}: {
  room: Room | null;
  onClose: () => void;
  onSave: () => void;
}) {
  const [name, setName] = useState(room?.name ?? "");
  const [thermostat, setThermostat] = useState(room?.thermostat_entity_id ?? "");
  const [sysTemp, setSysTemp] = useState<string>(room?.system_wide_temp?.toString() ?? "");
  const [holdover, setHoldover] = useState(room?.presence_holdover_hours?.toString() ?? "2");
  const [includeThermoSensor, setIncludeThermoSensor] = useState(room?.include_thermostat_sensor ?? false);
  const [notes, setNotes] = useState(room?.notes ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const save = async () => {
    if (!name.trim() || !thermostat.trim()) { setError("Name and thermostat are required"); return; }
    setSaving(true);
    try {
      const payload = {
        name: name.trim(),
        thermostat_entity_id: thermostat.trim(),
        system_wide_temp: sysTemp ? parseFloat(sysTemp) : null,
        presence_holdover_hours: parseFloat(holdover) || 0,
        include_thermostat_sensor: includeThermoSensor,
        notes,
      };
      if (room) await updateRoom(room.id, payload);
      else await createRoom(payload);
      onSave();
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
          <label className="form-label">Room name</label>
          <input className="form-control" value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Master Bedroom" />
        </div>

        <div className="form-group">
          <label className="form-label">Thermostat (climate entity)</label>
          <EntityPicker domain="climate" placeholder="Search climate entities…" onSelect={setThermostat} />
          {thermostat && <div className="tag" style={{ marginTop: ".35rem" }}>{thermostat}</div>}
        </div>

        <div className="form-group">
          <label className="form-label">System-wide temperature (°F) — used when presence detected</label>
          <input className="form-control" type="number" step="0.5" value={sysTemp} onChange={e => setSysTemp(e.target.value)} placeholder="e.g. 72" />
        </div>

        <div className="form-group">
          <label className="form-label">Presence holdover (hours, 0 = disabled)</label>
          <input className="form-control" type="number" step="0.5" min="0" value={holdover} onChange={e => setHoldover(e.target.value)} />
        </div>

        <div className="form-group">
          <label style={{ display: "flex", alignItems: "center", gap: ".5rem", cursor: "pointer" }}>
            <input type="checkbox" checked={includeThermoSensor} onChange={e => setIncludeThermoSensor(e.target.checked)} />
            <span className="form-label" style={{ marginBottom: 0 }}>Include thermostat's own sensor in room average</span>
          </label>
        </div>

        <div className="form-group">
          <label className="form-label">Notes</label>
          <textarea className="form-control" rows={2} value={notes} onChange={e => setNotes(e.target.value)} />
        </div>

        <div className="modal-footer">
          <button className="btn btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

function EntityTagList({
  label,
  items,
  domain,
  onAdd,
  onRemove,
}: {
  label: string;
  items: string[];
  domain: string;
  onAdd: (id: string) => void;
  onRemove: (id: string) => void;
}) {
  return (
    <div style={{ marginBottom: "1rem" }}>
      <div className="form-label">{label}</div>
      <EntityPicker domain={domain} onSelect={onAdd} />
      <div className="tag-list">
        {items.map(id => (
          <span key={id} className="tag">
            {id}
            <button className="tag-remove" onClick={() => onRemove(id)}>×</button>
          </span>
        ))}
      </div>
    </div>
  );
}

function RoomDetail({ room, onBack, onRefresh }: { room: Room; onBack: () => void; onRefresh: () => void }) {
  const sensors = room.sensors?.map(s => s.entity_id) ?? [];
  const vents = room.vents?.map(v => v.entity_id) ?? [];
  const presence = room.presence_sensors?.map(p => p.entity_id) ?? [];

  const doAdd = async (type: "sensor" | "vent" | "presence", id: string) => {
    if (type === "sensor") await addSensor(room.id, id);
    if (type === "vent") await addVent(room.id, id);
    if (type === "presence") await addPresence(room.id, id);
    onRefresh();
  };
  const doRemove = async (type: "sensor" | "vent" | "presence", id: string) => {
    if (type === "sensor") await removeSensor(room.id, id);
    if (type === "vent") await removeVent(room.id, id);
    if (type === "presence") await removePresence(room.id, id);
    onRefresh();
  };

  return (
    <div>
      <div className="page-header">
        <div>
          <button className="btn btn-secondary btn-sm" onClick={onBack} style={{ marginBottom: ".5rem" }}>← Back</button>
          <div className="page-title">{room.name}</div>
          <div className="page-subtitle font-mono">{room.thermostat_entity_id}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-title">Room details</div>
        <div className="stat-row"><span className="stat-label">System-wide temp</span><span className="stat-value">{room.system_wide_temp != null ? `${room.system_wide_temp}°F` : "—"}</span></div>
        <div className="stat-row"><span className="stat-label">Presence holdover</span><span className="stat-value">{room.presence_holdover_hours}h</span></div>
        <div className="stat-row"><span className="stat-label">Thermostat sensor included</span><span className="stat-value">{room.include_thermostat_sensor ? "Yes" : "No"}</span></div>
        {room.notes && <div className="stat-row"><span className="stat-label">Notes</span><span className="stat-value">{room.notes}</span></div>}
      </div>

      <div className="card" style={{ marginTop: "1rem" }}>
        <div className="card-title">Sensors, vents & presence</div>
        <EntityTagList
          label="Temperature sensors (sensor.*)"
          items={sensors}
          domain="sensor"
          onAdd={id => doAdd("sensor", id)}
          onRemove={id => doRemove("sensor", id)}
        />
        <hr className="divider" />
        <EntityTagList
          label="Flair vents (cover.*)"
          items={vents}
          domain="cover"
          onAdd={id => doAdd("vent", id)}
          onRemove={id => doRemove("vent", id)}
        />
        <hr className="divider" />
        <EntityTagList
          label="Presence/motion sensors (binary_sensor.*)"
          items={presence}
          domain="binary_sensor"
          onAdd={id => doAdd("presence", id)}
          onRemove={id => doRemove("presence", id)}
        />
      </div>
    </div>
  );
}

export default function Rooms() {
  const [rooms, setRooms] = useState<Room[]>([]);
  const [loading, setLoading] = useState(true);
  const [showModal, setShowModal] = useState(false);
  const [editRoom, setEditRoom] = useState<Room | null>(null);
  const [detailRoom, setDetailRoom] = useState<Room | null>(null);

  const load = async () => {
    const r = await getRooms();
    setRooms(r);
    setLoading(false);
    // Refresh detail view if open
    if (detailRoom) {
      const updated = r.find(x => x.id === detailRoom.id);
      if (updated) {
        // fetch full detail
        const { getRoom } = await import("../api");
        setDetailRoom(await getRoom(updated.id));
      }
    }
  };

  useEffect(() => { load(); }, []);

  if (loading) return <div className="loading"><div className="spinner" /> Loading rooms…</div>;

  if (detailRoom) {
    return (
      <RoomDetail
        room={detailRoom}
        onBack={() => setDetailRoom(null)}
        onRefresh={async () => {
          const { getRoom } = await import("../api");
          setDetailRoom(await getRoom(detailRoom.id));
          load();
        }}
      />
    );
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">Rooms</div>
          <div className="page-subtitle">{rooms.length} room{rooms.length !== 1 ? "s" : ""} configured</div>
        </div>
        <button className="btn btn-primary" onClick={() => { setEditRoom(null); setShowModal(true); }}>
          + Add room
        </button>
      </div>

      {rooms.length === 0 ? (
        <div className="card"><div className="empty-state"><p>No rooms yet. Click <strong>+ Add room</strong> to get started.</p></div></div>
      ) : (
        <div className="card-grid">
          {rooms.map(room => (
            <div className="card" key={room.id} style={{ cursor: "pointer" }} onClick={() => {
              import("../api").then(({ getRoom }) => getRoom(room.id).then(setDetailRoom));
            }}>
              <div className="flex-between">
                <div className="card-title" style={{ marginBottom: ".25rem" }}>{room.name}</div>
                <button
                  className="btn btn-secondary btn-sm"
                  onClick={async e => {
                    e.stopPropagation();
                    if (confirm(`Delete room "${room.name}"?`)) {
                      await deleteRoom(room.id);
                      load();
                    }
                  }}
                >
                  Delete
                </button>
              </div>
              <div className="font-mono text-muted" style={{ marginBottom: ".75rem" }}>{room.thermostat_entity_id}</div>
              <div className="stat-row">
                <span className="stat-label">System temp</span>
                <span className="stat-value">{room.system_wide_temp != null ? `${room.system_wide_temp}°F` : "—"}</span>
              </div>
              <div className="stat-row">
                <span className="stat-label">Presence holdover</span>
                <span className="stat-value">{room.presence_holdover_hours > 0 ? `${room.presence_holdover_hours}h` : "Disabled"}</span>
              </div>
              <div style={{ marginTop: ".5rem" }}>
                <button className="btn btn-secondary btn-sm" onClick={e => { e.stopPropagation(); setEditRoom(room); setShowModal(true); }}>
                  Edit
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {showModal && (
        <RoomModal
          room={editRoom}
          onClose={() => setShowModal(false)}
          onSave={() => { setShowModal(false); load(); }}
        />
      )}
    </div>
  );
}
