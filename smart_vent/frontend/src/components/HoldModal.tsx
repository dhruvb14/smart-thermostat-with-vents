import { useMemo, useState } from "react";
import { setOverride, clearOverride, type Room, type RoomOverrideHold } from "../api";
import { useUnit } from "../contexts";
import { Frozen } from "../ci";
import { formatCountdown } from "../countdown";

interface Props {
  rooms: Room[];
  // Pre-select this room (control opened from a specific room card / schedule
  // section). Omitted from the page-level entry points — the user picks.
  initialRoom?: string;
  // Live holds keyed by room_id (from getOverrides), so the modal can show
  // and cancel the selected room's existing hold.
  holds: Record<string, RoomOverrideHold>;
  onClose: () => void;
  // Fired after any successful change so the caller can refetch state.
  onChanged: () => void;
}

// The preset durations the feature offers (#576). The backend caps
// duration_hours at 8 — there is deliberately no larger option.
const DURATION_PRESETS = [1, 2, 4, 6, 8];

/**
 * Temporary-hold create/replace modal (Issue #576).
 *
 * One shared modal for every entry point (Dashboard, Rooms, Schedules): pick a
 * room, a target temperature, and a preset duration, and hold — or cancel the
 * room's existing hold. A hold overrides the room's schedules and presence
 * until it expires, then deletes itself; nothing else about the room is
 * changed. Temperatures are typed in display units and submitted raw — the
 * backend converts at the write boundary (#231).
 */
export default function HoldModal({ rooms, initialRoom, holds, onClose, onChanged }: Props) {
  const { toDisplay, displayBound, unitLabel, fmtTemp } = useUnit();
  const firstRoom = initialRoom ?? rooms[0]?.id ?? "";
  const [selected, setSelected] = useState(firstRoom);
  const [temp, setTemp] = useState(() => {
    const hold = holds[firstRoom];
    return String(hold ? toDisplay(hold.target_temp) : toDisplay(72));
  });
  const [duration, setDuration] = useState("2");
  const [allowEco, setAllowEco] = useState(() => holds[firstRoom]?.respect_eco ?? false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const existing = useMemo(() => holds[selected], [holds, selected]);

  const selectRoom = (roomId: string) => {
    setSelected(roomId);
    setError("");
    const hold = holds[roomId];
    if (hold) {
      setTemp(String(toDisplay(hold.target_temp)));
      setAllowEco(hold.respect_eco);
    }
  };

  const handleSave = async () => {
    if (!selected) {
      setError("Please choose a room.");
      return;
    }
    const t = parseFloat(temp);
    // Bounds via displayBound, not a raw toDisplay of the °F limit (#521):
    // rounding moves the bound outward, so toDisplay(40) = 4.4 °C converts
    // back to 39.92 °F and the backend refuses the advertised minimum.
    const minTemp = displayBound(40, "min");
    const maxTemp = displayBound(90, "max");
    if (isNaN(t) || t < minTemp || t > maxTemp) {
      setError(
        `Hold temperature must be between ${minTemp.toFixed(1)}${unitLabel} ` +
          `and ${maxTemp.toFixed(1)}${unitLabel}`
      );
      return;
    }
    setBusy(true);
    setError("");
    try {
      // Raw display value — the backend's _to_f converts (#231).
      await setOverride(selected, t, parseFloat(duration), allowEco);
      onChanged();
      onClose();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to set hold");
    } finally {
      setBusy(false);
    }
  };

  const handleCancelHold = async () => {
    setBusy(true);
    setError("");
    try {
      await clearOverride(selected);
      onChanged();
      onClose();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to cancel hold");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" data-testid="hold-modal">
        <div className="modal-title">
          {existing ? "Temporary hold active" : "Set temporary hold"}
        </div>
        <p style={{ marginBottom: "1rem" }}>
          Hold <strong>one room</strong> at an exact temperature for a fixed time — e.g. to shake
          off a temperature swing. The hold takes precedence over the room's schedules and presence,
          then deletes itself when the time is up. Nothing else about the room is changed.
        </p>

        <div className="form-group">
          <label className="form-label" htmlFor="hold-room">
            Room
          </label>
          <select
            id="hold-room"
            className="form-control"
            value={selected}
            onChange={(e) => selectRoom(e.target.value)}
            disabled={busy}
          >
            {rooms.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
                {holds[r.id] ? " — hold active" : ""}
              </option>
            ))}
          </select>
        </div>

        {existing && (
          <p style={{ marginBottom: "1rem" }}>
            This room is held at <strong>{fmtTemp(existing.target_temp)}</strong> — ends in{" "}
            <Frozen>{formatCountdown(existing.ends_in_seconds)}</Frozen>. Saving replaces the hold;
            cancelling returns the room to its schedules and presence now.
          </p>
        )}

        <div className="form-group">
          <label className="form-label" htmlFor="hold-target-temp">
            Hold temperature ({unitLabel})
          </label>
          <input
            id="hold-target-temp"
            className="form-control"
            type="number"
            step="0.5"
            min={displayBound(40, "min")}
            max={displayBound(90, "max")}
            value={temp}
            onChange={(e) => setTemp(e.target.value)}
            disabled={busy}
          />
        </div>

        <div className="form-group">
          <label className="form-label" htmlFor="hold-duration">
            Hold for
          </label>
          <select
            id="hold-duration"
            className="form-control"
            value={duration}
            onChange={(e) => setDuration(e.target.value)}
            disabled={busy}
          >
            {DURATION_PRESETS.map((h) => (
              <option key={h} value={String(h)}>
                {h === 1 ? "1 hour" : `${h} hours`}
              </option>
            ))}
          </select>
          <div className="form-hint">
            The hold expires and deletes itself after this long (8 hours at most).
          </div>
        </div>

        <div className="form-group">
          <label
            className="form-label"
            htmlFor="hold-allow-eco"
            style={{ display: "flex", alignItems: "center", gap: ".5rem", cursor: "pointer" }}
          >
            <input
              id="hold-allow-eco"
              type="checkbox"
              checked={allowEco}
              onChange={(e) => setAllowEco(e.target.checked)}
              disabled={busy}
            />
            Allow Eco Mode to relax this hold
          </label>
          <div className="form-hint">
            Off (the default), the hold runs to its exact target for its full duration — Eco Mode
            never adjusts it. On, Eco Mode may relax the hold's target on extreme days, exactly like
            a scheduled room; a thermostat-level Eco suspension or a disabled Eco config still
            prevents relaxation.
          </div>
        </div>

        {error && (
          <div className="badge badge-red" style={{ marginBottom: "1rem" }}>
            {error}
          </div>
        )}

        <div className="modal-footer">
          <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
            Close
          </button>
          {existing && (
            <button
              className="btn btn-danger"
              onClick={handleCancelHold}
              disabled={busy}
              data-testid="hold-modal-cancel-hold"
            >
              {busy ? "Working…" : "Cancel hold"}
            </button>
          )}
          <button
            className="btn btn-primary"
            onClick={handleSave}
            disabled={busy}
            data-testid="hold-modal-save"
          >
            {busy ? "Working…" : existing ? "Replace hold" : "Set hold"}
          </button>
        </div>
      </div>
    </div>
  );
}
