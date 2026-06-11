import { useEffect, useState } from "react";
import { getThermostatHealth, type ThermostatHealth, type UnavailableThermostat } from "../api";

/**
 * Surfaces thermostat unavailability on the Dashboard (Issue #267),
 * mirroring the stale-sensors banner.
 *
 * Polls ``/api/thermostat-health`` every 30 s. Renders nothing while every
 * registered thermostat is reachable; renders a red card listing the
 * unavailable ones otherwise. While a thermostat is unavailable the engine
 * cannot supervise its zone at all — no cycle timeout, no closed-vent
 * watchdog — so this is the loudest banner on the page.
 */
export default function UnavailableThermostatsBanner() {
  const [health, setHealth] = useState<ThermostatHealth | null>(null);

  useEffect(() => {
    let cancelled = false;
    const refresh = () => {
      getThermostatHealth()
        .then((h) => {
          if (!cancelled) setHealth(h);
        })
        .catch(() => {
          /* network blip — leave previous state in place */
        });
    };
    refresh();
    const interval = setInterval(refresh, 30000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  if (!health || health.thermostats.length === 0) return null;

  const n = health.thermostats.length;

  return (
    <div
      className="card"
      role="alert"
      data-testid="unavailable-thermostats-banner"
      style={{ borderLeft: "3px solid #dc2626", marginBottom: "1rem" }}
    >
      <div style={{ display: "flex", alignItems: "baseline", gap: ".5rem" }}>
        <strong>
          ⚠ {n} thermostat{n === 1 ? "" : "s"} unavailable in Home Assistant
        </strong>
      </div>
      <div className="text-sm" style={{ marginTop: ".5rem", color: "var(--gray-700)" }}>
        While a thermostat is unavailable the engine cannot supervise its zone — cycles are not
        monitored and vents are not adjusted. Check the device's power and connectivity, or the Home
        Assistant integration that provides it.
      </div>
      <ul style={{ margin: "0.5rem 0 0", paddingLeft: "1.25rem" }}>
        {health.thermostats.map((t) => (
          <li key={t.thermostat_entity_id} style={{ marginBottom: ".25rem" }}>
            <strong>{t.name || t.thermostat_entity_id}</strong>{" "}
            <code>{t.thermostat_entity_id}</code>{" "}
            <span className="text-sm text-muted">({describe(t)})</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function describe(t: UnavailableThermostat): string {
  const age =
    t.reason === "not_in_cache" || t.unavailable_seconds === null
      ? "never seen by HA"
      : `unavailable ${formatAge(t.unavailable_seconds)}`;
  if (!t.cycle_running) return age;
  if (t.abort_after_min <= 0) {
    return `${age} — a cycle is running and will NOT be auto-aborted (abort disabled on the Thermostats page)`;
  }
  return `${age} — the running cycle aborts after ${t.abort_after_min} min and all vents re-open`;
}

function formatAge(seconds: number): string {
  const minutes = Math.round(seconds / 60);
  if (minutes < 1) return "for under a minute";
  if (minutes < 60) return `for ${minutes} min`;
  return `for ${(minutes / 60).toFixed(1)} h`;
}
