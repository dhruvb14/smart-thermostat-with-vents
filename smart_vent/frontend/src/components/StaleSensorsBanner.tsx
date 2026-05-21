import { useEffect, useState } from "react";
import { getSensorHealth, type SensorHealth } from "../api";

/**
 * Surfaces sensor-staleness on the Dashboard (Issue #211).
 *
 * Polls ``/api/sensor-health`` every 30 s. Renders nothing when every
 * configured room sensor is fresh; renders an amber card listing the stale
 * sensors otherwise. A control loop that quietly trusts a dead battery is the
 * worst failure mode this app has, so the banner is deliberately loud.
 */
export default function StaleSensorsBanner() {
  const [health, setHealth] = useState<SensorHealth | null>(null);

  useEffect(() => {
    let cancelled = false;
    const refresh = () => {
      getSensorHealth()
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

  if (!health || health.rooms.length === 0) return null;

  const total = health.rooms.reduce((n, r) => n + r.stale_sensors.length, 0);

  return (
    <div
      className="card"
      role="alert"
      data-testid="stale-sensors-banner"
      style={{ borderLeft: "3px solid #f59e0b", marginBottom: "1rem" }}
    >
      <div style={{ display: "flex", alignItems: "baseline", gap: ".5rem" }}>
        <strong>
          ⚠ {total} sensor{total === 1 ? "" : "s"} not reporting
        </strong>
        <span className="text-sm text-muted">
          (threshold: {Math.round(health.stale_after_min)} min — configurable on the Settings page)
        </span>
      </div>
      <div className="text-sm" style={{ marginTop: ".5rem", color: "var(--gray-700)" }}>
        These rooms are running on partial sensor coverage; check the device batteries or
        connectivity. The engine excludes stale readings from the room temperature average so
        control decisions are not driven by them.
      </div>
      <ul style={{ margin: "0.5rem 0 0", paddingLeft: "1.25rem" }}>
        {health.rooms.map((room) => (
          <li key={room.room_id} style={{ marginBottom: ".25rem" }}>
            <strong>{room.room_name}</strong>{" "}
            {room.stale_sensors.map((s, i) => (
              <span key={s.entity_id}>
                {i > 0 && ", "}
                <code>{s.entity_id}</code>{" "}
                <span className="text-sm text-muted">({formatAge(s)})</span>
              </span>
            ))}
          </li>
        ))}
      </ul>
    </div>
  );
}

function formatAge(s: { age_seconds: number | null; reason: string }): string {
  if (s.reason === "not_in_cache" || s.age_seconds === null) return "never seen by HA";
  const minutes = Math.round(s.age_seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = minutes / 60;
  if (hours < 24) return `${hours.toFixed(1)} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}
