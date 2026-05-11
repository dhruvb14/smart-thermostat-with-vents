import { useEffect, useState } from "react";
import { getSettings, type VacationMode } from "../api";
import VacationModeModal from "./VacationModeModal";

function formatReturnAt(isoStr: string | null): string {
  if (!isoStr) return "";
  try {
    return new Date(isoStr).toLocaleString();
  } catch {
    return isoStr;
  }
}

export default function VacationModeBanner() {
  const [vacationMode, setVacationMode] = useState<VacationMode | null>(null);
  const [showModal, setShowModal] = useState(false);

  useEffect(() => {
    getSettings()
      .then((s) => setVacationMode(s.vacation_mode))
      .catch(() => {});
  }, []);

  if (!vacationMode?.enabled) return null;

  return (
    <>
      <div
        className="vacation-mode-banner"
        role="alert"
        onClick={() => setShowModal(true)}
        style={{ cursor: "pointer" }}
      >
        <div className="vacation-mode-banner-text">
          <strong>Vacation mode active</strong> — schedules and presence triggers are paused.
          Returning {formatReturnAt(vacationMode.return_at)}.
        </div>
        <div className="vacation-mode-banner-actions">
          <button
            className="btn-secondary"
            onClick={(e) => {
              e.stopPropagation();
              setShowModal(true);
            }}
          >
            Manage
          </button>
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
