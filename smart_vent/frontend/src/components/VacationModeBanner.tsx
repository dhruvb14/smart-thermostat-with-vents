// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useRef, useState } from "react";
import { dismissVacationMissedNotice, getSettings, type VacationMode } from "../api";
import { vacationState } from "../vacation";
import { notifyStateChanged, onStateChanged } from "../stateEvents";
import VacationModeModal from "./VacationModeModal";
import { useTimeZone } from "../contexts";
import { Frozen } from "../ci";

/** The scheduled banner turns into a heads-up this long before the start (#728). */
const HEADS_UP_MS = 2 * 60 * 60 * 1000;

/** "1 h 40 min" / "25 min" — how long until an instant, rounded up to the minute. */
function untilLabel(ms: number): string {
  const total = Math.max(1, Math.ceil(ms / 60_000));
  const h = Math.floor(total / 60);
  const m = total % 60;
  if (h === 0) return `${m} min`;
  return m === 0 ? `${h} h` : `${h} h ${m} min`;
}

/**
 * Global vacation notices (Issues #363, #728).
 *
 * Rendered once in ``App.tsx`` above ``<main>`` so it sits above the page title
 * on every route. Up to four cards, each with its own test id:
 * - ``vacation-mode-banner`` — active (green border), as before;
 * - ``vacation-scheduled-banner`` — a trip is scheduled (blue border), turning
 *   amber with a countdown from two hours before the start;
 * - ``welcome-home-banner`` — a welcome-home window is open;
 * - ``vacation-missed-banner`` — a trip passed while Plenum was offline, with a
 *   Dismiss button.
 *
 * It is mounted once for the life of the app, so it refetches whenever vacation
 * mode changes anywhere — the Dashboard's modal, another device, the tick that
 * starts or ends a trip — instead of only on mount (#678).
 */
export default function VacationModeBanner() {
  const [vacationMode, setVacationMode] = useState<VacationMode | null>(null);
  const [showModal, setShowModal] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const latestRequest = useRef(0);
  const tz = useTimeZone();

  const load = useCallback(() => {
    // Refetches can overlap (mount, this tab's own change, the WebSocket echo
    // of it); only the newest may land, so a slow older response cannot bring
    // back a banner the user just dismissed.
    const request = ++latestRequest.current;
    getSettings()
      .then((s) => {
        if (request === latestRequest.current) setVacationMode(s.vacation_mode);
      })
      .catch(() => {});
  }, []);

  useEffect(() => load(), [load]);
  useEffect(() => onStateChanged("vacation-mode", load), [load]);
  // The heads-up countdown and the welcome-home window are relative to now.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);

  if (!vacationMode) return null;
  const state = vacationState(vacationMode);
  const welcomeUntil =
    vacationMode.welcome_home_until && new Date(vacationMode.welcome_home_until).getTime() > now
      ? vacationMode.welcome_home_until
      : null;
  const missed = vacationMode.missed ?? null;
  if (state === "off" && !welcomeUntil && !missed) return null;

  const open = (e?: React.MouseEvent) => {
    e?.stopPropagation();
    setShowModal(true);
  };
  const manage = (
    <button className="btn btn-secondary" onClick={open}>
      Manage
    </button>
  );
  const headerStyle = {
    display: "flex",
    alignItems: "baseline",
    justifyContent: "space-between",
    gap: ".5rem",
    flexWrap: "wrap" as const,
  };
  const lineStyle = { marginTop: ".5rem", color: "var(--gray-700)" };

  const startMs = vacationMode.start_at ? new Date(vacationMode.start_at).getTime() : null;
  const headsUp = state === "scheduled" && startMs !== null && startMs - now <= HEADS_UP_MS;

  return (
    <>
      {missed && (
        <div
          className="card"
          role="alert"
          data-testid="vacation-missed-banner"
          style={{ borderLeft: "3px solid var(--orange)", marginBottom: "1rem" }}
        >
          <div style={headerStyle}>
            <strong>✈ Scheduled vacation missed</strong>
            <button
              className="btn btn-secondary"
              onClick={() =>
                dismissVacationMissedNotice()
                  .then((updated) => {
                    setVacationMode(updated);
                    notifyStateChanged("vacation-mode");
                  })
                  .catch(() => {})
              }
            >
              Dismiss
            </button>
          </div>
          <div className="text-sm" style={lineStyle} title={tz.tooltip(missed.start_at)}>
            {`A scheduled vacation (${tz.format(missed.start_at)} – ${tz.format(missed.return_at)}) was missed while Plenum was offline, so it never started.`}
          </div>
        </div>
      )}

      {state === "active" && (
        <div
          className="card"
          role="alert"
          data-testid="vacation-mode-banner"
          onClick={() => open()}
          style={{ borderLeft: "3px solid var(--green)", marginBottom: "1rem", cursor: "pointer" }}
        >
          <div style={headerStyle}>
            <strong>✈ Vacation mode active</strong>
            {manage}
          </div>
          <div
            className="text-sm"
            style={lineStyle}
            title={vacationMode.return_at ? tz.tooltip(vacationMode.return_at) : undefined}
          >
            Schedules and presence triggers are paused. Returning{" "}
            {vacationMode.return_at ? tz.format(vacationMode.return_at) : ""}.
          </div>
        </div>
      )}

      {state === "scheduled" && (
        <div
          className="card"
          role="status"
          data-testid="vacation-scheduled-banner"
          onClick={() => open()}
          style={{
            borderLeft: `3px solid var(${headsUp ? "--orange" : "--blue"})`,
            marginBottom: "1rem",
            cursor: "pointer",
          }}
        >
          <div style={headerStyle}>
            <strong>✈ Vacation scheduled</strong>
            {manage}
          </div>
          {headsUp && startMs !== null && vacationMode.start_at && (
            <div className="text-sm" style={lineStyle} title={tz.tooltip(vacationMode.start_at)}>
              <Frozen>
                {`Vacation mode starts in ${untilLabel(startMs - now)} (${tz.format(vacationMode.start_at, "time", { hour: "numeric", minute: "2-digit" })}).`}
              </Frozen>
            </div>
          )}
          <div
            className="text-sm"
            style={lineStyle}
            title={vacationMode.start_at ? tz.tooltip(vacationMode.start_at) : undefined}
          >
            {`Starts ${vacationMode.start_at ? tz.format(vacationMode.start_at) : ""}, returns ${vacationMode.return_at ? tz.format(vacationMode.return_at) : ""}. Until then everything runs as normal.`}
          </div>
        </div>
      )}

      {welcomeUntil && (
        <div
          className="card"
          role="status"
          data-testid="welcome-home-banner"
          style={{ borderLeft: "3px solid var(--green)", marginBottom: "1rem" }}
        >
          <strong>🏠 Welcome home</strong>
          <div className="text-sm" style={lineStyle} title={tz.tooltip(welcomeUntil)}>
            {`Rooms are coming back to temperature until ${tz.format(welcomeUntil, "time", { hour: "numeric", minute: "2-digit" })}.`}
          </div>
        </div>
      )}

      {showModal && (
        <VacationModeModal
          current={vacationMode}
          onClose={() => setShowModal(false)}
          onChanged={(updated) => {
            setVacationMode(updated);
            setShowModal(false);
          }}
        />
      )}
    </>
  );
}
