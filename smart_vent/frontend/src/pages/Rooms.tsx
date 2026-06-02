import { useEffect, useRef, useState } from "react";
import {
  getRooms,
  getRoom,
  createRoom,
  updateRoom,
  deleteRoom,
  addSensor,
  removeSensor,
  addVent,
  removeVent,
  updateVentControlMethod,
  testVent,
  addPresence,
  removePresence,
  clearPresenceHoldover,
  getThermostats,
  getEntityStates,
  getOutsideTempEntity,
  getRoomActiveStatuses,
  getSensorHealth,
  type StaleSensor,
  CONTROL_METHOD_LABELS,
  type ControlMethod,
  type Room,
  type RoomVent,
  type ThermostatConfig,
  type EntityState,
  type RoomActiveStatus,
} from "../api";
import { useSystem, useUnit } from "../contexts";
import EntityPicker from "../components/EntityPicker";

// ---------------------------------------------------------------------------
// Room create / edit modal (name, thermostat, presence config)
// ---------------------------------------------------------------------------
function RoomModal({
  room,
  thermostats,
  onClose,
  onSave,
}: {
  room: Room | null;
  thermostats: ThermostatConfig[];
  onClose: () => void;
  onSave: (saved: Room) => void;
}) {
  const { toDisplay, toDisplayDelta, unitLabel, fmtTemp } = useUnit();
  const [name, setName] = useState(room?.name ?? "");
  const [thermostat, setThermostat] = useState(room?.thermostat_entity_id ?? "");
  const [sysTemp, setSysTemp] = useState(
    room?.system_wide_temp != null ? String(toDisplay(room.system_wide_temp)) : ""
  );
  const [holdover, setHoldover] = useState(room?.presence_holdover_hours?.toString() ?? "2");
  const [includeThermoSensor, setIncludeThermoSensor] = useState(
    room?.include_thermostat_sensor ?? false
  );
  const [tempOffset, setTempOffset] = useState(String(toDisplayDelta(room?.temp_offset ?? 0)));
  const [notes, setNotes] = useState(room?.notes ?? "");
  // Ambient-aware presence suppression / pre-cool / pre-heat (Issue #248).
  const [ambientEnabled, setAmbientEnabled] = useState(room?.ambient_suppression_enabled ?? false);
  const [ambientMode, setAmbientMode] = useState<"any_presence" | "off_schedule_only">(
    room?.ambient_suppression_mode ?? "any_presence"
  );
  const [ambientMinDiff, setAmbientMinDiff] = useState(
    String(toDisplayDelta(room?.ambient_suppression_min_differential ?? 5))
  );
  const [ambientDeadband, setAmbientDeadband] = useState(
    String(toDisplayDelta(room?.ambient_suppression_deadband ?? 2))
  );
  const [ambientWindow, setAmbientWindow] = useState(
    String(room?.ambient_suppression_off_schedule_window_min ?? 60)
  );
  // The feature is inert without an outside temperature sensor, so the controls
  // are disabled until one is configured (system-wide setting).
  const [hasOutsideSensor, setHasOutsideSensor] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    getOutsideTempEntity()
      .then((r) => {
        if (!cancelled) setHasOutsideSensor(!!r?.entity_id);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  // Widened deadband must be >= the selected thermostat's deadband. The
  // thermostat stores it in °F; show/compare in display units.
  const selectedThermo = thermostats.find((t) => t.thermostat_entity_id === thermostat);
  const thermoDeadbandDisplay = toDisplayDelta(selectedThermo?.deadband ?? 0.5);

  const save = async () => {
    if (!name.trim()) {
      setError("Room name is required");
      return;
    }
    if (!thermostat.trim()) {
      setError("Thermostat is required");
      return;
    }
    const holdoverVal = parseFloat(holdover);
    if (isNaN(holdoverVal) || holdoverVal < 0) {
      setError("Presence holdover must be >= 0");
      return;
    }

    if (sysTemp) {
      const st = parseFloat(sysTemp);
      if (isNaN(st) || st < toDisplay(40) || st > toDisplay(90)) {
        setError(
          `Presence-triggered temperature must be between ${fmtTemp(40)} and ${fmtTemp(90)}`
        );
        return;
      }
    }

    // Pre-cool/pre-heat (Issue #248): the two delta fields are validated the
    // same way the backend does, so a bad value is caught before the request.
    const minDiffVal = parseFloat(ambientMinDiff);
    if (isNaN(minDiffVal) || minDiffVal < 0) {
      setError("Pre-cool/pre-heat: minimum outside difference must be 0 or greater");
      return;
    }
    const deadbandVal = parseFloat(ambientDeadband);
    if (isNaN(deadbandVal) || deadbandVal < thermoDeadbandDisplay - 1e-6) {
      setError(
        `Pre-cool/pre-heat: widened deadband must be at least the thermostat's deadband ` +
          `(${thermoDeadbandDisplay}${unitLabel})`
      );
      return;
    }
    if (ambientMode === "off_schedule_only") {
      const windowVal = parseInt(ambientWindow, 10);
      if (isNaN(windowVal) || windowVal < 0) {
        setError("Pre-cool/pre-heat: schedule window must be 0 or greater");
        return;
      }
    }

    setSaving(true);
    setError("");
    try {
      // Temperatures are sent in DISPLAY units; the backend converts to °F
      // on the write boundary via _to_f / _delta_to_f.
      const payload = {
        name: name.trim(),
        thermostat_entity_id: thermostat.trim(),
        system_wide_temp: sysTemp ? parseFloat(sysTemp) : null,
        presence_holdover_hours: parseFloat(holdover) || 0,
        include_thermostat_sensor: includeThermoSensor,
        temp_offset: parseFloat(tempOffset) || 0,
        ambient_suppression_enabled: ambientEnabled,
        ambient_suppression_mode: ambientMode,
        ambient_suppression_min_differential: parseFloat(ambientMinDiff) || 0,
        ambient_suppression_deadband: parseFloat(ambientDeadband) || 0,
        ambient_suppression_off_schedule_window_min: parseInt(ambientWindow, 10) || 0,
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
    <div className="modal-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-title">{room ? "Edit Room" : "New Room"}</div>
        {error && (
          <div className="badge badge-red" style={{ marginBottom: "1rem" }}>
            {error}
          </div>
        )}

        <div className="form-group">
          <label className="form-label" htmlFor="room-name">
            Room name *
          </label>
          <input
            id="room-name"
            className="form-control"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Master Bedroom"
            autoFocus
          />
        </div>

        <div className="form-group">
          <label className="form-label" htmlFor="room-thermostat">
            Thermostat *
          </label>
          {thermostats.length === 0 ? (
            <div className="form-hint" style={{ color: "var(--orange)" }}>
              No thermostats registered yet. Go to the <strong>Thermostats</strong> page first to
              register and name your thermostats.
            </div>
          ) : (
            <select
              id="room-thermostat"
              className="form-control"
              value={thermostat}
              onChange={(e) => setThermostat(e.target.value)}
            >
              <option value="">— select a thermostat —</option>
              {thermostats.map((tc) => (
                <option key={tc.thermostat_entity_id} value={tc.thermostat_entity_id}>
                  {tc.name} ({tc.thermostat_entity_id})
                </option>
              ))}
            </select>
          )}
        </div>

        <hr className="divider" />

        <div className="form-group">
          <label className="form-label" htmlFor="room-sys-temp">
            Presence-triggered temperature ({unitLabel})
            <span className="text-muted" style={{ fontWeight: 400, marginLeft: ".5rem" }}>
              — used when motion/presence detected, no active schedule
            </span>
          </label>
          <input
            id="room-sys-temp"
            className="form-control"
            type="number"
            step="0.5"
            value={sysTemp}
            onChange={(e) => setSysTemp(e.target.value)}
            placeholder="e.g. 72"
          />
        </div>

        <div className="form-group">
          <label className="form-label" htmlFor="room-holdover">
            Presence holdover (hours)
            <span className="text-muted" style={{ fontWeight: 400, marginLeft: ".5rem" }}>
              — keep room active this long after last motion; 0 = disabled
            </span>
          </label>
          <input
            id="room-holdover"
            className="form-control"
            type="number"
            step="0.5"
            min="0"
            value={holdover}
            onChange={(e) => setHoldover(e.target.value)}
          />
        </div>

        <div className="form-group">
          <label
            htmlFor="room-include-thermo"
            style={{ display: "flex", alignItems: "center", gap: ".5rem", cursor: "pointer" }}
          >
            <input
              id="room-include-thermo"
              type="checkbox"
              checked={includeThermoSensor}
              onChange={(e) => setIncludeThermoSensor(e.target.checked)}
            />
            <span>Include thermostat's built-in sensor in room temperature average</span>
          </label>
        </div>

        <div className="form-group">
          <label className="form-label" htmlFor="room-temp-offset">
            Temperature offset ({unitLabel})
          </label>
          <input
            id="room-temp-offset"
            className="form-control"
            type="number"
            step="0.5"
            value={tempOffset}
            onChange={(e) => setTempOffset(e.target.value)}
          />
          <div className="form-hint">
            Compensates for temperature drift after the vent closes. The offset is added to the
            room's measured temperature before comparing to the schedule target — so the vent closes
            earlier, leaving room for drift.
            <br />
            <strong>Example:</strong> your schedule targets 70°F in cooling, but this room always
            ends up at 67°F even after the vent closes. Set offset to <strong>+3</strong> — the
            system will now close the vent when the room reads 67°F (67 + 3 = 70, "at target"), and
            the room drifts to ~70°F instead of 67°F. Leave at 0 if the room reaches its target
            accurately.
          </div>
        </div>

        <hr className="divider" />

        <div className="form-group">
          <label
            htmlFor="room-ambient-enabled"
            style={{ display: "flex", alignItems: "center", gap: ".5rem", cursor: "pointer" }}
          >
            <input
              id="room-ambient-enabled"
              type="checkbox"
              checked={ambientEnabled}
              disabled={!hasOutsideSensor}
              onChange={(e) => setAmbientEnabled(e.target.checked)}
            />
            <span>
              Skip presence heating/cooling when the weather will do it for me (pre-cool / pre-heat)
            </span>
          </label>
          {!hasOutsideSensor && (
            <div className="form-hint" style={{ color: "var(--orange)" }}>
              Add an outside temperature sensor on the <strong>Metrics</strong> page to use this —
              it has no effect without one.
            </div>
          )}
          <div className="form-hint">
            When presence would heat or cool this room but the outside air will carry it to target
            on its own, Plenum skips the HVAC and lets the room drift.
            <br />
            <strong>Example:</strong> your night schedule holds this room at 68{unitLabel} and ends
            at 7am. At 7:30am someone walks in and presence wants 70{unitLabel}. It is already
            warmer outside, so the room will reach 70{unitLabel} on its own — Plenum skips the
            heater. The moment the room actually reaches 70{unitLabel}, normal heating/cooling
            resumes.
          </div>
        </div>

        {ambientEnabled && (
          <>
            <div className="form-group">
              <label className="form-label" htmlFor="room-ambient-mode">
                When to apply
              </label>
              <select
                id="room-ambient-mode"
                className="form-control"
                value={ambientMode}
                onChange={(e) =>
                  setAmbientMode(e.target.value as "any_presence" | "off_schedule_only")
                }
              >
                <option value="any_presence">Any presence</option>
                <option value="off_schedule_only">Only after a schedule ends</option>
              </select>
              <div className="form-hint">
                <strong>Example:</strong> &ldquo;Only after a schedule ends&rdquo; skips presence
                heat/cool just after a schedule block ends (the 7am case) but behaves normally
                midday. &ldquo;Any presence&rdquo; applies the check every time presence activates
                the room.
              </div>
            </div>

            {ambientMode === "off_schedule_only" && (
              <div className="form-group">
                <label className="form-label" htmlFor="room-ambient-window">
                  Schedule window (minutes)
                </label>
                <input
                  id="room-ambient-window"
                  className="form-control"
                  type="number"
                  step="5"
                  min="0"
                  value={ambientWindow}
                  onChange={(e) => setAmbientWindow(e.target.value)}
                />
                <div className="form-hint">
                  How long after a schedule ends this still applies. <strong>Example:</strong> 60 =
                  applies until 8am for a schedule ending at 7am.
                </div>
              </div>
            )}

            <div className="form-group">
              <label className="form-label" htmlFor="room-ambient-mindiff">
                Minimum outside difference ({unitLabel})
              </label>
              <input
                id="room-ambient-mindiff"
                className="form-control"
                type="number"
                step="0.5"
                min="0"
                value={ambientMinDiff}
                onChange={(e) => setAmbientMinDiff(e.target.value)}
              />
              <div className="form-hint">
                How far past the target the outside temperature must be before coasting.
                <br />
                <strong>Example:</strong> set to 5{unitLabel} with a 70{unitLabel} target, Plenum
                only skips heating when it is at least 75{unitLabel} outside, and only skips cooling
                when it is at most 65{unitLabel} outside. If it is only 71{unitLabel} out, that is
                too little push, so it heats normally. Bigger = only coast when the weather strongly
                favors it.
              </div>
            </div>

            <div className="form-group">
              <label className="form-label" htmlFor="room-ambient-deadband">
                Widened deadband ({unitLabel})
              </label>
              <input
                id="room-ambient-deadband"
                className="form-control"
                type="number"
                step="0.5"
                min={thermoDeadbandDisplay}
                value={ambientDeadband}
                onChange={(e) => setAmbientDeadband(e.target.value)}
              />
              <div className="form-hint">
                <strong>Example:</strong> your thermostat&rsquo;s deadband is{" "}
                {thermoDeadbandDisplay}
                {unitLabel}. This widened deadband must be at least that — set it the same or
                higher. With a 70{unitLabel} target and a widened deadband of 3{unitLabel}, while
                coasting up Plenum will not call for heat until the room drops 3{unitLabel} below
                target. The instant the room rises past 70{unitLabel}, normal control resumes — it
                cools at the normal deadband, not the widened one. The thermostat&rsquo;s min/max
                setpoint always overrides this.
              </div>
            </div>
          </>
        )}

        <div className="form-group">
          <label className="form-label" htmlFor="room-notes">
            Notes
          </label>
          <textarea
            id="room-notes"
            className="form-control"
            rows={2}
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
          />
        </div>

        <div className="modal-footer">
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
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
        {items.length > 0 && <span className="badge badge-blue">{items.length}</span>}
      </div>
      <p className="text-sm text-muted" style={{ marginBottom: ".75rem" }}>
        {description}
      </p>

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
          {items.map((id) => (
            <span key={id} className="tag">
              <span className="font-mono">{id}</span>
              <button className="tag-remove" title="Remove" onClick={() => onRemove(id)}>
                ×
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Vents table — per-vent control method + test actions
// ---------------------------------------------------------------------------
function VentTable({
  roomId,
  vents,
  onChanged,
}: {
  roomId: string;
  vents: RoomVent[];
  onChanged: () => Promise<void>;
}) {
  return (
    <div style={{ marginBottom: "1.5rem" }}>
      <div style={{ display: "flex", alignItems: "center", gap: ".5rem", marginBottom: ".3rem" }}>
        <span style={{ fontSize: "1.2rem" }}>💨</span>
        <span style={{ fontWeight: 700, fontSize: "1rem" }}>Vents</span>
        {vents.length > 0 && <span className="badge badge-blue">{vents.length}</span>}
      </div>
      <p className="text-sm text-muted" style={{ marginBottom: ".75rem" }}>
        Vents in this room, controlled as cover entities. When the room hits target, the system
        closes these vents. Each vent can use a different control method depending on which services
        its HA integration exposes.
      </p>

      <EntityPicker
        domain="cover"
        placeholder="Search vents (cover.*)…"
        onSelect={async (id) => {
          await addVent(roomId, id);
          await onChanged();
        }}
      />

      {vents.length === 0 ? (
        <p className="text-sm text-muted" style={{ marginTop: ".5rem", fontStyle: "italic" }}>
          No vents added yet — search above to add one. Rooms without vents are sensor-only and
          still participate in schedules and presence control.
        </p>
      ) : (
        <div style={{ marginTop: ".75rem", overflowX: "auto" }}>
          <table className="vent-table" style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", borderBottom: "1px solid var(--border, #ddd)" }}>
                <th style={{ padding: ".4rem .5rem" }}>Entity</th>
                <th style={{ padding: ".4rem .5rem" }}>Control method</th>
                <th style={{ padding: ".4rem .5rem" }}>Test</th>
                <th style={{ padding: ".4rem .5rem" }}></th>
              </tr>
            </thead>
            <tbody>
              {vents.map((v) => (
                <VentRow key={v.id} vent={v} onChanged={onChanged} />
              ))}
            </tbody>
          </table>
          <p className="text-sm text-muted" style={{ marginTop: ".5rem", fontStyle: "italic" }}>
            Not sure which method your vent uses? Try it in Home Assistant → Developer Tools →
            Actions against the entity to see which service works.
          </p>
        </div>
      )}
    </div>
  );
}

function VentRow({ vent, onChanged }: { vent: RoomVent; onChanged: () => Promise<void> }) {
  const [method, setMethod] = useState<ControlMethod>(vent.control_method);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState<"open" | "close" | null>(null);
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);

  const onChangeMethod = async (next: ControlMethod) => {
    setMethod(next);
    setSaving(true);
    setStatus(null);
    try {
      await updateVentControlMethod(vent.room_id, vent.entity_id, next);
      await onChanged();
    } catch (err) {
      setStatus({ ok: false, msg: `Save failed: ${(err as Error).message}` });
      setMethod(vent.control_method);
    } finally {
      setSaving(false);
    }
  };

  const runTest = async (direction: "open" | "close") => {
    setTesting(direction);
    setStatus(null);
    try {
      await testVent(vent.entity_id, method, direction);
      setStatus({ ok: true, msg: `${direction === "open" ? "Open" : "Close"} command accepted` });
    } catch (err) {
      setStatus({ ok: false, msg: (err as Error).message });
    } finally {
      setTesting(null);
    }
  };

  return (
    <tr style={{ borderBottom: "1px solid var(--border, #eee)", verticalAlign: "top" }}>
      <td style={{ padding: ".5rem" }}>
        <span className="font-mono text-sm">{vent.entity_id}</span>
      </td>
      <td style={{ padding: ".5rem" }}>
        <select
          value={method}
          onChange={(e) => onChangeMethod(e.target.value as ControlMethod)}
          disabled={saving}
          style={{ minWidth: "20rem" }}
        >
          {(Object.keys(CONTROL_METHOD_LABELS) as ControlMethod[]).map((m) => (
            <option key={m} value={m}>
              {CONTROL_METHOD_LABELS[m]}
            </option>
          ))}
        </select>
        {status && (
          <div
            className="text-sm"
            style={{
              marginTop: ".25rem",
              color: status.ok ? "var(--success, #1a7f37)" : "var(--danger, #b3261e)",
            }}
          >
            {status.ok ? "✓" : "✗"} {status.msg}
          </div>
        )}
      </td>
      <td style={{ padding: ".5rem", whiteSpace: "nowrap" }}>
        <button
          className="btn btn-sm"
          onClick={() => runTest("open")}
          disabled={testing !== null || saving}
          style={{ marginRight: ".25rem" }}
        >
          {testing === "open" ? "…" : "Test open"}
        </button>
        <button
          className="btn btn-sm"
          onClick={() => runTest("close")}
          disabled={testing !== null || saving}
        >
          {testing === "close" ? "…" : "Test close"}
        </button>
      </td>
      <td style={{ padding: ".5rem" }}>
        <button
          className="tag-remove"
          title="Remove"
          onClick={async () => {
            await removeVent(vent.room_id, vent.entity_id);
            await onChanged();
          }}
        >
          ×
        </button>
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Room configure view (sensors, vents, presence)
// ---------------------------------------------------------------------------
function RoomConfigure({
  room,
  thermostats,
  onBack,
  onRoomUpdated,
}: {
  room: Room;
  thermostats: ThermostatConfig[];
  onBack: () => void;
  onRoomUpdated: (r: Room) => void;
}) {
  const { fmtTemp, toDisplayDelta, unitLabel } = useUnit();
  const [editOpen, setEditOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const sensors = room.sensors?.map((s) => s.entity_id) ?? [];
  const vents = room.vents?.map((v) => v.entity_id) ?? [];
  const presence = room.presence_sensors?.map((p) => p.entity_id) ?? [];

  const refresh = async () => {
    const updated = await getRoom(room.id);
    onRoomUpdated(updated);
  };

  const wrap = (label: string, fn: (id: string) => Promise<unknown>) => async (id: string) => {
    setBusy(label);
    try {
      await fn(id);
      await refresh();
    } finally {
      setBusy(null);
    }
  };

  return (
    <div>
      {/* Header */}
      <div style={{ marginBottom: "1.25rem" }}>
        <button
          className="btn btn-secondary btn-sm"
          onClick={onBack}
          style={{ marginBottom: ".75rem" }}
        >
          ← All rooms
        </button>
        <div className="flex-between">
          <div>
            <div className="page-title">{room.name}</div>
            <div className="text-muted" style={{ marginTop: ".2rem", fontSize: ".85rem" }}>
              {thermostats.find((t) => t.thermostat_entity_id === room.thermostat_entity_id)
                ?.name || room.thermostat_entity_id}{" "}
              <span className="font-mono" style={{ fontSize: ".75rem", color: "var(--gray-400)" }}>
                ({room.thermostat_entity_id})
              </span>
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
            <strong>{sensors.length}</strong>{" "}
            <span className="text-muted">temp sensor{sensors.length !== 1 ? "s" : ""}</span>
          </span>
          <span className="text-muted">·</span>
          <span className="text-sm">
            <strong>{vents.length}</strong>{" "}
            <span className="text-muted">vent{vents.length !== 1 ? "s" : ""}</span>
          </span>
          <span className="text-muted">·</span>
          <span className="text-sm">
            <strong>{presence.length}</strong>{" "}
            <span className="text-muted">presence sensor{presence.length !== 1 ? "s" : ""}</span>
          </span>
          {room.system_wide_temp != null && (
            <>
              <span className="text-muted">·</span>
              <span className="text-sm text-muted">
                Presence temp: <strong>{fmtTemp(room.system_wide_temp)}</strong>
              </span>
            </>
          )}
          {room.temp_offset !== 0 && (
            <>
              <span className="text-muted">·</span>
              <span className="text-sm text-muted">
                Offset:{" "}
                <strong>
                  {room.temp_offset > 0 ? "+" : ""}
                  {toDisplayDelta(room.temp_offset)}
                  {unitLabel}
                </strong>
              </span>
            </>
          )}
        </div>
      </div>

      {/* Warnings */}
      {sensors.length === 0 && (
        <div
          className="card"
          style={{ marginBottom: "1rem", borderColor: "#f59e0b", background: "#fffbeb" }}
        >
          <p className="text-sm" style={{ color: "#92400e" }}>
            ⚠ No temperature sensors — this room will be skipped during HVAC cycles.
          </p>
        </div>
      )}
      {vents.length === 0 && (
        <div
          className="card"
          style={{
            marginBottom: "1rem",
            borderColor: "var(--gray-200)",
            background: "var(--gray-50)",
          }}
        >
          <p className="text-sm text-muted">
            ℹ Sensor-only room — no vents configured. This room still participates in schedules and
            presence-based HVAC control; its target temperature contributes to the thermostat
            setpoint. Only vent actuation is skipped.
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
          onAdd={wrap("Adding sensor…", (id) => addSensor(room.id, id))}
          onRemove={wrap("Removing sensor…", (id) => removeSensor(room.id, id))}
        />

        <hr className="divider" />

        <VentTable roomId={room.id} vents={room.vents ?? []} onChanged={refresh} />

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
          thermostats={thermostats}
          onClose={() => setEditOpen(false)}
          onSave={async () => {
            setEditOpen(false);
            await refresh();
          }}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Live state + countdown helpers
// ---------------------------------------------------------------------------

function formatStaleAge(s: StaleSensor): string {
  if (s.reason === "not_in_cache" || s.age_seconds === null) return "never seen by HA";
  const minutes = Math.round(s.age_seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = s.age_seconds / 3600;
  if (hours < 24) return `${hours.toFixed(1)} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

function formatCountdown(totalSeconds: number): string {
  if (totalSeconds <= 0) return "ending…";
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function sourceLabel(source: RoomActiveStatus["source"]): string {
  switch (source) {
    case "schedule":
      return "Schedule";
    case "presence":
      return "Presence";
    case "override":
      return "Override";
    default:
      return "—";
  }
}

function ventLabel(state: EntityState): string {
  // Flair vents report current_tilt_position; standard covers use current_position
  const pos = (state.attributes.current_tilt_position ?? state.attributes.current_position) as
    | number
    | undefined;
  if (pos !== undefined) {
    if (pos === 100) return "Open";
    if (pos === 0) return "Closed";
    return `${pos}%`;
  }
  // Fallback to cover state string
  const s = state.state;
  if (s === "open") return "Open";
  if (s === "closed") return "Closed";
  return s;
}

// ---------------------------------------------------------------------------
// Room list card
// ---------------------------------------------------------------------------
function RoomCard({
  room,
  thermostats,
  status,
  statusFetchedAt,
  staleSensors,
  onConfigure,
  onEdit,
  onDelete,
  onClearPresence,
}: {
  room: Room;
  thermostats: ThermostatConfig[];
  status: RoomActiveStatus | null;
  statusFetchedAt: number; // Date.now() when status was fetched
  staleSensors: StaleSensor[];
  onConfigure: () => void;
  onEdit: () => void;
  onDelete: () => void;
  onClearPresence: () => void;
}) {
  const { enabled: systemEnabled } = useSystem();
  const { fmtTemp, toDisplayDelta, unitLabel } = useUnit();
  const sensorIds = room.sensors?.map((s) => s.entity_id) ?? [];
  const ventIds = room.vents?.map((v) => v.entity_id) ?? [];
  const presenceIds = room.presence_sensors?.map((p) => p.entity_id) ?? [];
  const tc = thermostats.find((t) => t.thermostat_entity_id === room.thermostat_entity_id);
  const missing = sensorIds.length === 0 || ventIds.length === 0;

  const [states, setStates] = useState<Record<string, EntityState | null>>({});
  // Tick every second for live countdown
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    const allIds = [...sensorIds, ...ventIds, ...presenceIds];
    if (allIds.length === 0) return;
    getEntityStates(allIds)
      .then(setStates)
      .catch(() => {});
    // Intentionally scoped to room.id: refetching on every sensor/vent/presence
    // mutation would thrash HA during inline edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [room.id]);

  // Derived live values
  const temps = sensorIds
    .map((id) => states[id]?.numeric)
    .filter((v): v is number => v !== null && v !== undefined);
  const avgTemp =
    temps.length > 0 ? (temps.reduce((a, b) => a + b, 0) / temps.length).toFixed(1) : null;

  const occupied = presenceIds.some((id) => states[id]?.state === "on");
  const hasPresenceData =
    presenceIds.length > 0 && presenceIds.some((id) => states[id] !== undefined);

  // Countdown: compute elapsed since status was fetched
  const elapsedSeconds = Math.floor((Date.now() - statusFetchedAt) / 1000);
  const endsIn =
    status?.ends_in_seconds != null ? Math.max(0, status.ends_in_seconds - elapsedSeconds) : null;
  const nextIn =
    status?.next_schedule_in_seconds != null
      ? Math.max(0, status.next_schedule_in_seconds - elapsedSeconds)
      : null;

  const isActive = status && status.source !== "idle";
  const isDisabled = !systemEnabled;

  return (
    <div className={`card ${isDisabled ? "room-card-disabled" : ""}`}>
      <div className="flex-between" style={{ marginBottom: ".5rem" }}>
        <div className="card-title" style={{ marginBottom: 0 }}>
          {room.name}
          {staleSensors.length > 0 && (
            <span
              className="badge badge-orange"
              data-testid={`stale-badge-${room.id}`}
              title={staleSensors.map((s) => `${s.entity_id} — ${formatStaleAge(s)}`).join("\n")}
              style={{ marginLeft: ".5rem", verticalAlign: "middle", fontSize: ".75rem" }}
            >
              ⚠ {staleSensors.length} stale sensor{staleSensors.length === 1 ? "" : "s"}
            </span>
          )}
        </div>
        <button className="btn btn-danger btn-sm" onClick={onDelete}>
          Delete
        </button>
      </div>

      <div className="text-muted" style={{ marginBottom: ".875rem", fontSize: ".82rem" }}>
        {tc?.name ? (
          <>
            {tc.name}{" "}
            <span className="font-mono" style={{ fontSize: ".75rem" }}>
              ({room.thermostat_entity_id})
            </span>
          </>
        ) : (
          <span className="font-mono">{room.thermostat_entity_id}</span>
        )}
      </div>

      {/* Active status row */}
      <div className="room-status-row">
        {/* Global Off badge — shown alongside schedule info, not instead of it */}
        {isDisabled && <span className="room-status-disabled">⏸ Global Off</span>}

        {status == null ? (
          <span className="room-status-loading">…</span>
        ) : (
          <>
            {/* Target temp — grayed out when system disabled */}
            <span
              className={`room-status-target ${isActive && !isDisabled ? "room-status-active" : "room-status-idle"}`}
            >
              {isActive ? `🎯 ${fmtTemp(status.target_temp!)}` : "Not active"}
            </span>

            {/* Active via */}
            {isActive && <span className="room-status-via">via {sourceLabel(status.source)}</span>}

            {/* Ends in countdown */}
            {isActive && endsIn != null && (
              <span className="room-status-ends">ends in {formatCountdown(endsIn)}</span>
            )}

            {/* Clear presence button — shown whenever a holdover timer is running */}
            {status.presence_holdover_active && (
              <button
                className="btn btn-sm btn-outline-danger"
                style={{ marginLeft: "auto", padding: "0 .5rem", fontSize: ".75rem" }}
                onClick={async () => {
                  await clearPresenceHoldover(room.id);
                  onClearPresence();
                }}
              >
                Clear presence
              </button>
            )}

            {/* Next schedule */}
            {status.next_schedule_label && nextIn != null && (
              <span className="room-status-next">
                {isActive ? "then" : "next"}{" "}
                <strong>{fmtTemp(status.next_schedule_target!)}</strong>{" "}
                {status.next_schedule_label}
                {nextIn > 0 && (
                  <span className="room-status-next-timer"> ({formatCountdown(nextIn)})</span>
                )}
              </span>
            )}
          </>
        )}
      </div>

      {/* Live state strip */}
      <div className="room-live-strip">
        {/* Temperature */}
        <div className="room-live-item">
          <span className="room-live-label">🌡 Temp</span>
          <span className="room-live-value">
            {avgTemp !== null ? fmtTemp(Number(avgTemp)) : sensorIds.length === 0 ? "—" : "…"}
          </span>
        </div>

        {/* Presence */}
        {presenceIds.length > 0 && (
          <div className="room-live-item">
            <span className="room-live-label">🚶 Presence</span>
            <span
              className={`room-live-value ${hasPresenceData ? (occupied ? "live-occupied" : "live-unoccupied") : ""}`}
            >
              {!hasPresenceData ? "…" : occupied ? "Occupied" : "Unoccupied"}
              {/* Holdover countdown when presence is the active source */}
              {occupied && status?.source === "presence" && endsIn != null && (
                <span className="room-status-next-timer" style={{ marginLeft: ".35rem" }}>
                  (resets in {formatCountdown(endsIn)})
                </span>
              )}
            </span>
          </div>
        )}

        {/* Vents */}
        {ventIds.length > 0 && (
          <div className="room-live-item">
            <span className="room-live-label">💨 Vents</span>
            <div className="room-live-vents">
              {ventIds.map((id) => {
                const s = states[id];
                return (
                  <span key={id} className="room-vent-pill" title={id}>
                    {s ? ventLabel(s) : "…"}
                  </span>
                );
              })}
            </div>
          </div>
        )}
      </div>

      {/* Entity counts */}
      <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap", marginBottom: ".875rem" }}>
        <span className={`badge ${sensorIds.length > 0 ? "badge-green" : "badge-red"}`}>
          🌡 {sensorIds.length} sensor{sensorIds.length !== 1 ? "s" : ""}
        </span>
        <span className={`badge ${ventIds.length > 0 ? "badge-blue" : "badge-gray"}`}>
          💨 {ventIds.length} vent{ventIds.length !== 1 ? "s" : ""}
        </span>
        <span className={`badge ${presenceIds.length > 0 ? "badge-green" : "badge-gray"}`}>
          🚶 {presenceIds.length} presence
        </span>
        {room.temp_offset !== 0 && (
          <span className="badge badge-orange" title="Temperature offset active">
            offset {room.temp_offset > 0 ? "+" : ""}
            {toDisplayDelta(room.temp_offset)}
            {unitLabel}
          </span>
        )}
      </div>

      {missing && (
        <p className="text-sm" style={{ color: "#b45309", marginBottom: ".75rem" }}>
          ⚠ {sensorIds.length === 0 ? "No temperature sensors" : "No vents"} — configure below.
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
  const [thermostats, setThermostats] = useState<ThermostatConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [showModal, setShowModal] = useState(false);
  const [editRoom, setEditRoom] = useState<Room | null>(null);
  const [configRoom, setConfigRoom] = useState<Room | null>(null);
  const [statuses, setStatuses] = useState<Record<string, RoomActiveStatus>>({});
  const [statusFetchedAt, setStatusFetchedAt] = useState<number>(Date.now());
  // room_id → stale sensors. Drives the per-card badge (Issue #211).
  const [staleByRoom, setStaleByRoom] = useState<Record<string, StaleSensor[]>>({});
  const roomsRef = useRef<Room[]>([]);

  const refreshSensorHealth = async () => {
    try {
      const h = await getSensorHealth();
      const map: Record<string, StaleSensor[]> = {};
      for (const r of h.rooms) map[r.room_id] = r.stale_sensors;
      setStaleByRoom(map);
    } catch {
      // ignore — banner-style information, not critical for the page to render
    }
  };

  const fetchStatuses = async (roomList: Room[]) => {
    if (roomList.length === 0) return;
    try {
      const s = await getRoomActiveStatuses(roomList.map((r) => r.id));
      setStatuses(s);
      setStatusFetchedAt(Date.now());
    } catch {
      // ignore
    }
  };

  const load = async () => {
    const [list, tcs] = await Promise.all([getRooms(), getThermostats()]);
    const detailed = await Promise.all(list.map((r) => getRoom(r.id)));
    setRooms(detailed);
    roomsRef.current = detailed;
    setThermostats(tcs);
    setLoading(false);
    await Promise.all([fetchStatuses(detailed), refreshSensorHealth()]);
  };

  useEffect(() => {
    load();
    // Mount-only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-fetch statuses every 30s. Mount-only — uses ref to read latest rooms
  // without re-subscribing.
  useEffect(() => {
    const interval = setInterval(() => {
      fetchStatuses(roomsRef.current);
      refreshSensorHealth();
    }, 30_000);
    return () => clearInterval(interval);
  }, []);

  if (loading)
    return (
      <div className="loading">
        <div className="spinner" /> Loading rooms…
      </div>
    );

  // Configure view
  if (configRoom) {
    return (
      <RoomConfigure
        room={configRoom}
        thermostats={thermostats}
        onBack={() => {
          setConfigRoom(null);
          load();
        }}
        onRoomUpdated={(updated) => setConfigRoom(updated)}
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
        <button
          className="btn btn-primary"
          onClick={() => {
            setEditRoom(null);
            setShowModal(true);
          }}
        >
          + Add room
        </button>
      </div>

      {rooms.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <p>No rooms yet.</p>
            <p style={{ marginTop: ".5rem" }}>
              Click <strong>+ Add room</strong>, pick a thermostat, then configure its sensors and
              vents.
            </p>
          </div>
        </div>
      ) : (
        <div className="card-grid">
          {rooms.map((room) => (
            <RoomCard
              key={room.id}
              room={room}
              thermostats={thermostats}
              status={statuses[room.id] ?? null}
              statusFetchedAt={statusFetchedAt}
              staleSensors={staleByRoom[room.id] ?? []}
              onConfigure={() => setConfigRoom(room)}
              onEdit={() => {
                setEditRoom(room);
                setShowModal(true);
              }}
              onDelete={async () => {
                if (confirm(`Delete room "${room.name}"?`)) {
                  await deleteRoom(room.id);
                  load();
                }
              }}
              onClearPresence={() => fetchStatuses(roomsRef.current)}
            />
          ))}
        </div>
      )}

      {showModal && (
        <RoomModal
          room={editRoom}
          thermostats={thermostats}
          onClose={() => setShowModal(false)}
          onSave={async (saved) => {
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
