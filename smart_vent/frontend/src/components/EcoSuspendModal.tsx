import { useMemo, useState } from "react";
import { setEcoSuspend, clearEcoSuspend, type ThermostatConfig } from "../api";
import { toDatetimeLocalString } from "./datetimeLocal";

interface Props {
  thermostats: ThermostatConfig[];
  // Pre-select this thermostat (control opened from a specific zone card /
  // thermostat section). Omitted from the global entry points (banner,
  // page-level buttons) — the user picks.
  initialThermostat?: string;
  onClose: () => void;
  // Fired after any successful change so the caller can refetch state.
  onChanged: () => void;
}

function fmtLocal(isoStr: string | null | undefined): string {
  if (!isoStr) return "";
  const d = new Date(isoStr);
  return Number.isNaN(d.getTime()) ? isoStr : d.toLocaleString();
}

function thermostatLabel(tc: ThermostatConfig): string {
  return tc.name || tc.thermostat_entity_id;
}

/**
 * Eco Suspend create/edit modal (Issue #500).
 *
 * One shared modal for every entry point: pick which thermostat the suspension
 * lands on, pick the resume date/time, and suspend — or, when the selected
 * thermostat is already suspended, update the resume time or resume Eco now.
 * The standing Eco configuration is never modified; suspensions take effect
 * from the next cycle (a running cycle finishes under the Eco state it
 * started with).
 */
export default function EcoSuspendModal({
  thermostats,
  initialThermostat,
  onClose,
  onChanged,
}: Props) {
  const [selected, setSelected] = useState(
    initialThermostat ?? thermostats[0]?.thermostat_entity_id ?? ""
  );
  const [resumeAt, setResumeAt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const current = useMemo(
    () => thermostats.find((t) => t.thermostat_entity_id === selected),
    [thermostats, selected]
  );
  const activeUntil = current?.eco_suspend_until ?? null;

  const handleSuspend = async () => {
    if (!selected) {
      setError("Please choose a thermostat.");
      return;
    }
    if (!resumeAt) {
      setError("Please choose when Eco Mode should resume.");
      return;
    }
    const dt = new Date(resumeAt);
    if (isNaN(dt.getTime()) || dt <= new Date()) {
      setError("Resume date must be in the future.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await setEcoSuspend(selected, dt.toISOString());
      onChanged();
      onClose();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to suspend Eco Mode");
    } finally {
      setBusy(false);
    }
  };

  const handleResumeNow = async () => {
    setBusy(true);
    setError("");
    try {
      await clearEcoSuspend(selected);
      onChanged();
      onClose();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to resume Eco Mode");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-title">{activeUntil ? "Eco Mode suspended" : "Suspend Eco Mode"}</div>
        <p style={{ marginBottom: "1rem" }}>
          Temporarily turn Eco Mode's target relaxation <strong>off for one thermostat</strong> —
          e.g. while hosting guests — so every room in that zone runs to its real target. Eco
          resumes automatically at the date you pick; your Eco settings are not changed.
        </p>
        <p style={{ marginBottom: "1.25rem", color: "var(--gray-600)", fontSize: ".9rem" }}>
          Applies zone-wide, including rooms with a room-level Eco opt-in. A cycle already running
          finishes as-is; the change applies from the next cycle.
        </p>

        <div className="form-group">
          <label className="form-label" htmlFor="eco-suspend-thermostat">
            Thermostat
          </label>
          <select
            id="eco-suspend-thermostat"
            className="form-control"
            value={selected}
            onChange={(e) => {
              setSelected(e.target.value);
              setError("");
            }}
            disabled={busy}
          >
            {thermostats.map((tc) => (
              <option key={tc.thermostat_entity_id} value={tc.thermostat_entity_id}>
                {thermostatLabel(tc)}
                {tc.eco_suspend_until ? " — suspended" : tc.eco_mode_enabled ? "" : " — Eco off"}
              </option>
            ))}
          </select>
          {current && !current.eco_mode_enabled && !activeUntil && (
            <div className="form-hint">
              Eco Mode is not enabled on this thermostat, so suspending it only affects rooms with a
              room-level Eco opt-in.
            </div>
          )}
        </div>

        {activeUntil && (
          <p style={{ marginBottom: "1rem" }}>
            Eco Mode is suspended until <strong>{fmtLocal(activeUntil)}</strong>. Pick a new
            date/time to change it, or resume Eco now.
          </p>
        )}

        <div className="form-group">
          <label className="form-label" htmlFor="eco-suspend-resume-at">
            Resume Eco at
          </label>
          <input
            id="eco-suspend-resume-at"
            className="form-control"
            type="datetime-local"
            value={resumeAt}
            onChange={(e) => setResumeAt(e.target.value)}
            min={toDatetimeLocalString(new Date(Date.now() + 60_000))}
            disabled={busy}
          />
          <div className="form-hint">
            Eco Mode resumes automatically at this local date and time (from the next cycle).
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
          {activeUntil && (
            <button className="btn btn-danger" onClick={handleResumeNow} disabled={busy}>
              {busy ? "Working…" : "Resume Eco now"}
            </button>
          )}
          <button className="btn btn-primary" onClick={handleSuspend} disabled={busy}>
            {busy ? "Working…" : activeUntil ? "Update suspension" : "Suspend Eco"}
          </button>
        </div>
      </div>
    </div>
  );
}
