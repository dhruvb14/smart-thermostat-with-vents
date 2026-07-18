import { useState } from "react";
import { enableVacationMode, disableVacationMode, type VacationMode } from "../api";
import { toDatetimeLocalString } from "./datetimeLocal";

interface Props {
  current: VacationMode;
  onClose: () => void;
  onChanged: (updated: VacationMode) => void;
}

function formatReturnAt(isoStr: string | null): string {
  if (!isoStr) return "";
  // `new Date()` never throws — a malformed string yields an Invalid Date
  // whose toLocaleString() is the literal "Invalid Date", so the fallback to
  // the raw string must go through a NaN check, not try/catch.
  const d = new Date(isoStr);
  return Number.isNaN(d.getTime()) ? isoStr : d.toLocaleString();
}

export default function VacationModeModal({ current, onClose, onChanged }: Props) {
  const [confirmDisable, setConfirmDisable] = useState(false);
  const [returnAt, setReturnAt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const handleEnable = async () => {
    if (!returnAt) {
      setError("Please choose a return date and time.");
      return;
    }
    const dt = new Date(returnAt);
    if (isNaN(dt.getTime()) || dt <= new Date()) {
      setError("Return date must be in the future.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const updated = await enableVacationMode(dt.toISOString());
      onChanged(updated);
      onClose();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to enable vacation mode");
    } finally {
      setBusy(false);
    }
  };

  const handleDisable = async () => {
    setBusy(true);
    setError("");
    try {
      const updated = await disableVacationMode();
      onChanged(updated);
      onClose();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to disable vacation mode");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        {current.enabled ? (
          /* ── Dismiss / cancel flow ── */
          confirmDisable ? (
            <>
              <div className="modal-title">End vacation mode?</div>
              <p style={{ marginBottom: "1rem" }}>
                Your thermostats will return to <strong>normal schedule control immediately</strong>
                . Any rooms with active schedules will resume within one minute.
              </p>
              {error && (
                <div className="badge badge-red" style={{ marginBottom: "1rem" }}>
                  {error}
                </div>
              )}
              <div className="modal-footer">
                <button
                  className="btn btn-secondary"
                  onClick={() => setConfirmDisable(false)}
                  disabled={busy}
                >
                  Keep vacation mode
                </button>
                <button className="btn btn-danger" onClick={handleDisable} disabled={busy}>
                  {busy ? "Ending…" : "Yes, end vacation mode"}
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="modal-title">Vacation mode active</div>
              <p style={{ marginBottom: "1rem" }}>
                Vacation mode is <strong>active</strong> until{" "}
                <strong>{formatReturnAt(current.return_at)}</strong>. All room schedules, presence
                triggers, and overrides are paused. Each thermostat is being held within its
                configured safety limits.
              </p>
              <p style={{ marginBottom: "1rem", color: "var(--gray-600)", fontSize: ".9rem" }}>
                Normal scheduling will resume automatically at your return date. You can also end it
                early below.
              </p>
              <div className="modal-footer">
                <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
                  Close
                </button>
                <button
                  className="btn btn-danger"
                  onClick={() => setConfirmDisable(true)}
                  disabled={busy}
                >
                  End vacation mode early
                </button>
              </div>
            </>
          )
        ) : (
          /* ── Enable flow ── */
          <>
            <div className="modal-title">Enable vacation mode</div>
            <p style={{ marginBottom: "1rem" }}>
              While vacation mode is active, <strong>all room schedules</strong>, presence triggers,
              and temporary overrides are paused. Each thermostat will be held within its configured
              minimum and maximum setpoint limits until the return date, then{" "}
              <strong>normal scheduling resumes automatically</strong>.
            </p>
            <p style={{ marginBottom: "1.25rem", color: "var(--gray-600)", fontSize: ".9rem" }}>
              This applies to <strong>all thermostats</strong>. Each thermostat's hold strategy
              (range or single setpoint) can be configured under{" "}
              <em>Vacation mode hold strategy</em> on the Thermostats page.
            </p>

            <div className="form-group">
              <label className="form-label" htmlFor="vacation-return-at">
                Return date &amp; time
              </label>
              <input
                id="vacation-return-at"
                className="form-control"
                type="datetime-local"
                value={returnAt}
                onChange={(e) => setReturnAt(e.target.value)}
                min={toDatetimeLocalString(new Date(Date.now() + 60_000))}
              />
              <div className="form-hint">
                Vacation mode will end automatically at this local date and time and normal
                scheduling will resume within one minute.
              </div>
            </div>

            {error && (
              <div className="badge badge-red" style={{ marginBottom: "1rem" }}>
                {error}
              </div>
            )}

            <div className="modal-footer">
              <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={handleEnable} disabled={busy}>
                {busy ? "Enabling…" : "Enable vacation mode"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
