import { useEffect, useState } from "react";
import { getSettings, ackUnitChange, restartApp } from "../api";

export default function UnitChangeBanner() {
  const [visible, setVisible] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [dismissing, setDismissing] = useState(false);

  useEffect(() => {
    getSettings()
      .then((s) => setVisible(s.unit_change_ack_required))
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
      <span className="unit-change-banner-text">
        The temperature unit has changed. Review your temperature settings, then restart to apply.
      </span>
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
