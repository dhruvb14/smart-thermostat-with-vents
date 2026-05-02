import { useEffect, useState } from "react";
import { getSettings, ackUnitChange, restartApp } from "../api";

const AFFECTED_SETTINGS = [
  "Min / max setpoints (Thermostats page)",
  "Deadband and overshoot delta (Thermostats page)",
  "Schedule target temperatures (Schedules page)",
  "Presence-triggered temperatures (Rooms page)",
  "Temperature offsets (Rooms page)",
];

export default function UnitChangeBanner() {
  const [visible, setVisible] = useState(false);
  const [newUnit, setNewUnit] = useState<"F" | "C">("F");
  const [restarting, setRestarting] = useState(false);
  const [dismissing, setDismissing] = useState(false);

  useEffect(() => {
    getSettings()
      .then((s) => {
        setVisible(s.unit_change_ack_required);
        setNewUnit(s.temperature_unit);
      })
      .catch(() => {});
  }, []);

  if (!visible) return null;

  const handleRestart = async () => {
    if (restarting) return;
    setRestarting(true);
    try {
      await restartApp();
    } catch {
      setRestarting(false);
    }
  };

  const handleDismiss = async () => {
    if (dismissing) return;
    setDismissing(true);
    try {
      await ackUnitChange();
      setVisible(false);
    } catch {
      setDismissing(false);
    }
  };

  return (
    <div className="unit-change-banner" role="alert">
      <div className="unit-change-banner-text">
        <strong>Temperature unit changed to °{newUnit}.</strong> The following stored values are now
        displayed in the new unit — review them to confirm they still make sense, then restart
        Plenum to apply:
        <ul style={{ margin: ".4rem 0 0 1.2rem", padding: 0 }}>
          {AFFECTED_SETTINGS.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      </div>
      <div className="unit-change-banner-actions">
        <button onClick={handleRestart} disabled={restarting} className="btn-primary">
          {restarting ? "Restarting…" : "Restart Plenum"}
        </button>
        <button onClick={handleDismiss} disabled={dismissing} className="btn-secondary">
          {dismissing ? "…" : "I've reviewed my settings"}
        </button>
      </div>
    </div>
  );
}
