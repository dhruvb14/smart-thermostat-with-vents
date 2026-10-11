import { useCallback, useEffect, useRef, useState } from "react";
import { getSettings, type VacationMode } from "../api";
import { onStateChanged } from "../stateEvents";
import VacationModeModal from "./VacationModeModal";

function formatReturnAt(isoStr: string | null): string {
  if (!isoStr) return "";
  // `new Date()` never throws — a malformed string yields an Invalid Date
  // whose toLocaleString() is the literal "Invalid Date", so the fallback to
  // the raw string must go through a NaN check, not try/catch.
  const d = new Date(isoStr);
  return Number.isNaN(d.getTime()) ? isoStr : d.toLocaleString();
}

/**
 * Global "Vacation mode active" notice (Issue #363).
 *
 * Rendered once in ``App.tsx`` above ``<main>`` so it sits above the page title
 * on every route. Styled as a card with a green left border — visually parallel
 * to ``StaleSensorsBanner`` (which uses an amber border for warnings), but green
 * to signal an informational/active state rather than a problem. Clicking the
 * card (or the Manage button) opens the ``VacationModeModal``.
 *
 * It is mounted once for the life of the app, so it refetches whenever vacation
 * mode changes anywhere — the Dashboard's modal, another device, the return-time
 * expiry — instead of only on mount (#678).
 */
export default function VacationModeBanner() {
  const [vacationMode, setVacationMode] = useState<VacationMode | null>(null);
  const [showModal, setShowModal] = useState(false);
  const latestRequest = useRef(0);

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

  if (!vacationMode?.enabled) return null;

  return (
    <>
      <div
        className="card"
        role="alert"
        data-testid="vacation-mode-banner"
        onClick={() => setShowModal(true)}
        style={{ borderLeft: "3px solid var(--green)", marginBottom: "1rem", cursor: "pointer" }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "baseline",
            justifyContent: "space-between",
            gap: ".5rem",
            flexWrap: "wrap",
          }}
        >
          <strong>✈ Vacation mode active</strong>
          <button
            className="btn btn-secondary"
            onClick={(e) => {
              e.stopPropagation();
              setShowModal(true);
            }}
          >
            Manage
          </button>
        </div>
        <div className="text-sm" style={{ marginTop: ".5rem", color: "var(--gray-700)" }}>
          Schedules and presence triggers are paused. Returning{" "}
          {formatReturnAt(vacationMode.return_at)}.
        </div>
      </div>

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
