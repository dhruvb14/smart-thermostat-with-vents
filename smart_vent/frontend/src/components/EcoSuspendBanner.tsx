import { useCallback, useEffect, useState } from "react";
import { getThermostats, type ThermostatConfig } from "../api";
import EcoSuspendModal from "./EcoSuspendModal";

function fmtLocal(isoStr: string): string {
  const d = new Date(isoStr);
  return Number.isNaN(d.getTime()) ? isoStr : d.toLocaleString();
}

/**
 * Global "Eco suspended" notice (Issue #500).
 *
 * Rendered once in ``App.tsx`` alongside ``VacationModeBanner`` so it shows on
 * every page while ANY thermostat has an active Eco suspension. One banner
 * lists every suspended thermostat (never a stack of N banners). Green like
 * the vacation banner — Eco is the green feature — with the 🍃 icon telling
 * the two apart when both are active. Clicking anywhere on the card (or the
 * Manage button) opens the ``EcoSuspendModal`` to edit or cancel.
 */
export default function EcoSuspendBanner() {
  const [thermostats, setThermostats] = useState<ThermostatConfig[]>([]);
  const [showModal, setShowModal] = useState(false);

  const load = useCallback(() => {
    getThermostats()
      .then(setThermostats)
      .catch(() => {});
  }, []);

  useEffect(() => load(), [load]);

  const suspended = thermostats.filter((t) => t.eco_suspend_until != null);
  if (suspended.length === 0) return null;

  return (
    <>
      <div
        className="card"
        role="alert"
        data-testid="eco-suspend-banner"
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
          <strong>🍃 Eco Mode suspended</strong>
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
          {suspended.map((t, i) => (
            <span key={t.thermostat_entity_id}>
              {i > 0 && " · "}
              <strong>{t.name || t.thermostat_entity_id}</strong> resuming{" "}
              {fmtLocal(t.eco_suspend_until as string)}
            </span>
          ))}
        </div>
      </div>

      {showModal && (
        <EcoSuspendModal
          thermostats={thermostats}
          initialThermostat={suspended[0].thermostat_entity_id}
          onClose={() => setShowModal(false)}
          onChanged={load}
        />
      )}
    </>
  );
}
