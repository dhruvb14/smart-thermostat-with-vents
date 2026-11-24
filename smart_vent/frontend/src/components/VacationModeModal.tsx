// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from "react";
import { enableVacationMode, disableVacationMode, type VacationMode } from "../api";
import { vacationState } from "../vacation";
import { notifyStateChanged } from "../stateEvents";
import { useTimeZone } from "../contexts";
import Time, { ZoneHint } from "./Time";
import DstAdjustedHint from "./DstAdjustedHint";

interface Props {
  current: VacationMode;
  onClose: () => void;
  onChanged: (updated: VacationMode) => void;
}

type Confirm = "none" | "end" | "cancel";

/**
 * Vacation mode: enable now, schedule a trip for later (Issue #728), edit or
 * cancel a scheduled trip, and end an active one with or without welcome home.
 *
 * Every time here is typed and shown in the HOUSE's zone (#729).
 */
export default function VacationModeModal({ current, onClose, onChanged }: Props) {
  const state = vacationState(current);
  const tz = useTimeZone();
  const [confirm, setConfirm] = useState<Confirm>("none");
  // The date form shows for "off" (create) and for "Edit dates" on a
  // scheduled trip, prefilled with that trip's dates.
  const [editing, setEditing] = useState(state === "off");
  const [immediate, setImmediate] = useState(false);
  const [startAt, setStartAt] = useState(
    state === "scheduled" && current.start_at ? tz.toInput(current.start_at) : ""
  );
  const [returnAt, setReturnAt] = useState(
    state === "scheduled" && current.return_at ? tz.toInput(current.return_at) : ""
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const run = async (action: () => Promise<VacationMode>, failure: string) => {
    setBusy(true);
    setError("");
    try {
      const updated = await action();
      // `onChanged` only reaches the caller; the global banner and any other
      // open view hold their own copy of the state (#678).
      notifyStateChanged("vacation-mode");
      onChanged(updated);
      onClose();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : failure);
    } finally {
      setBusy(false);
    }
  };

  const handleSubmit = async () => {
    if (!immediate && !startAt) {
      setError("Please choose when vacation mode should start, or tick Start immediately.");
      return;
    }
    if (!returnAt) {
      setError("Please choose a return date and time.");
      return;
    }
    // Typed in the HOUSE's zone, not the browser's (#729).
    const ret = tz.fromInput(returnAt);
    if (!ret || new Date(ret.iso) <= new Date()) {
      setError("Return date must be in the future.");
      return;
    }
    let start: string | undefined;
    if (!immediate) {
      const parsed = tz.fromInput(startAt);
      if (!parsed || new Date(parsed.iso) <= new Date()) {
        setError("Start date must be in the future.");
        return;
      }
      if (new Date(parsed.iso) >= new Date(ret.iso)) {
        setError("Return date must be after the start date.");
        return;
      }
      start = parsed.iso;
    }
    await run(
      () => (start ? enableVacationMode(ret.iso, start) : enableVacationMode(ret.iso)),
      immediate ? "Failed to enable vacation mode" : "Failed to schedule vacation mode"
    );
  };

  const errorBadge = error && (
    <div className="badge badge-red" style={{ marginBottom: "1rem" }}>
      {error}
    </div>
  );

  // ── Active: end early, with or without welcome home ──
  if (state === "active") {
    return (
      <Backdrop onClose={onClose}>
        {confirm === "end" ? (
          <>
            <div className="modal-title">End vacation mode?</div>
            <p style={{ marginBottom: "1rem" }}>
              Your thermostats will return to <strong>normal schedule control immediately</strong>.
              Any rooms with active schedules will resume within one minute.
            </p>
            <p style={{ marginBottom: "1rem", color: "var(--gray-600)", fontSize: ".9rem" }}>
              <strong>Welcome home</strong> also starts now on every thermostat that has it on: each
              room is treated as occupied once, so it heats or cools to its presence temperature.
              Choose <em>End without welcome home</em> to skip that.
            </p>
            {errorBadge}
            <div className="modal-footer">
              <button
                className="btn btn-secondary"
                onClick={() => setConfirm("none")}
                disabled={busy}
              >
                Keep vacation mode
              </button>
              <button
                className="btn btn-secondary"
                onClick={() =>
                  run(() => disableVacationMode(false), "Failed to disable vacation mode")
                }
                disabled={busy}
              >
                End without welcome home
              </button>
              <button
                className="btn btn-danger"
                onClick={() => run(() => disableVacationMode(), "Failed to disable vacation mode")}
                disabled={busy}
              >
                {busy ? "Ending…" : "Yes, end vacation mode"}
              </button>
            </div>
          </>
        ) : (
          <>
            <div className="modal-title">Vacation mode active</div>
            <p style={{ marginBottom: "1rem" }}>
              Vacation mode is <strong>active</strong> until{" "}
              <strong>{current.return_at && <Time value={current.return_at} />}</strong>. All room
              schedules, presence triggers, and overrides are paused. Each thermostat is being held
              within its configured safety limits.
            </p>
            <p style={{ marginBottom: "1rem", color: "var(--gray-600)", fontSize: ".9rem" }}>
              Normal scheduling will resume automatically at your return date, with welcome home
              warming or cooling every room shortly before. You can also end it early below.
            </p>
            <div className="modal-footer">
              <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
                Close
              </button>
              <button className="btn btn-danger" onClick={() => setConfirm("end")} disabled={busy}>
                End vacation mode early
              </button>
            </div>
          </>
        )}
      </Backdrop>
    );
  }

  // ── Scheduled: summary, start now, cancel (with confirm) ──
  if (state === "scheduled" && !editing) {
    return (
      <Backdrop onClose={onClose}>
        {confirm === "cancel" ? (
          <>
            <div className="modal-title">Cancel scheduled vacation?</div>
            <p style={{ marginBottom: "1rem" }}>
              The trip is removed and vacation mode will not start. Nothing else changes.
            </p>
            {errorBadge}
            <div className="modal-footer">
              <button
                className="btn btn-secondary"
                onClick={() => setConfirm("none")}
                disabled={busy}
              >
                Keep it
              </button>
              <button
                className="btn btn-danger"
                onClick={() =>
                  run(() => disableVacationMode(), "Failed to cancel the scheduled vacation")
                }
                disabled={busy}
              >
                {busy ? "Cancelling…" : "Yes, cancel it"}
              </button>
            </div>
          </>
        ) : (
          <>
            <div className="modal-title">Vacation scheduled</div>
            <p style={{ marginBottom: "1rem" }}>
              Vacation mode starts{" "}
              <strong>{current.start_at && <Time value={current.start_at} />}</strong> and ends{" "}
              <strong>{current.return_at && <Time value={current.return_at} />}</strong>. Until then
              everything runs as normal; Plenum switches it on and off by itself.
            </p>
            {errorBadge}
            <div className="modal-footer">
              <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
                Close
              </button>
              <button
                className="btn btn-secondary"
                onClick={() => setConfirm("cancel")}
                disabled={busy}
              >
                Cancel scheduled vacation
              </button>
              <button
                className="btn btn-secondary"
                onClick={() => setEditing(true)}
                disabled={busy}
              >
                Edit dates
              </button>
              <button
                className="btn btn-primary"
                onClick={() =>
                  run(
                    () => enableVacationMode(current.return_at as string),
                    "Failed to start vacation mode"
                  )
                }
                disabled={busy || !current.return_at}
              >
                {busy ? "Starting…" : "Start now"}
              </button>
            </div>
          </>
        )}
      </Backdrop>
    );
  }

  // ── Create (off) or edit (scheduled) ──
  const editingScheduled = state === "scheduled";
  const minStart = tz.toInput(Date.now() + 60_000);
  const startParsed = !immediate && startAt ? tz.fromInput(startAt) : null;
  const minReturn = startParsed
    ? tz.toInput(new Date(startParsed.iso).getTime() + 60_000)
    : minStart;
  return (
    <Backdrop onClose={onClose}>
      <div className="modal-title">
        {editingScheduled ? "Edit scheduled vacation" : "Enable vacation mode"}
      </div>
      <p style={{ marginBottom: "1rem" }}>
        While vacation mode is active, <strong>all room schedules</strong>, presence triggers, and
        temporary overrides are paused. Each thermostat will be held within its configured minimum
        and maximum setpoint limits until the return date, then{" "}
        <strong>normal scheduling resumes automatically</strong>.
      </p>
      <p style={{ marginBottom: "1.25rem", color: "var(--gray-600)", fontSize: ".9rem" }}>
        This applies to <strong>all thermostats</strong>. Each thermostat's hold strategy (range or
        single setpoint) and its <em>Welcome home</em> warm-up are configured on the Thermostats
        page.
      </p>

      {!editingScheduled && (
        <div className="form-group">
          <label style={{ display: "flex", alignItems: "center", gap: ".5rem", cursor: "pointer" }}>
            <input
              id="vacation-start-immediately"
              type="checkbox"
              checked={immediate}
              onChange={(e) => setImmediate(e.target.checked)}
            />
            <span>Start immediately</span>
          </label>
        </div>
      )}

      {!immediate && (
        <div className="form-group">
          <label className="form-label" htmlFor="vacation-start-at">
            Start date &amp; time
          </label>
          <input
            id="vacation-start-at"
            className="form-control"
            type="datetime-local"
            value={startAt}
            onChange={(e) => setStartAt(e.target.value)}
            min={minStart}
          />
          <ZoneHint />
          <DstAdjustedHint value={startAt} />
          <div className="form-hint">
            Vacation mode starts by itself at this date and time in the house&apos;s time zone.
          </div>
        </div>
      )}

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
          min={minReturn}
        />
        <ZoneHint />
        <DstAdjustedHint value={returnAt} />
        <div className="form-hint">
          Vacation mode will end automatically at this date and time in the house&apos;s time zone,
          and normal scheduling will resume within one minute.
        </div>
      </div>

      {errorBadge}

      <div className="modal-footer">
        <button
          className="btn btn-secondary"
          onClick={editingScheduled ? () => setEditing(false) : onClose}
          disabled={busy}
        >
          {editingScheduled ? "Back" : "Cancel"}
        </button>
        <button className="btn btn-primary" onClick={handleSubmit} disabled={busy}>
          {busy
            ? "Saving…"
            : editingScheduled
              ? "Save dates"
              : immediate
                ? "Enable vacation mode"
                : "Schedule vacation"}
        </button>
      </div>
    </Backdrop>
  );
}

function Backdrop({ onClose, children }: { onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="modal-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">{children}</div>
    </div>
  );
}
