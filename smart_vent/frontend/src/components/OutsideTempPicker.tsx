import { useEffect, useState } from "react";
import { getOutsideTempEntity, setOutsideTempEntity } from "../api";
import { useUnit } from "../contexts";
import EntityPicker from "./EntityPicker";

/**
 * House-wide outdoor-temperature sensor picker.
 *
 * One sensor serves the whole home — it is NOT tied to an individual
 * thermostat. The reading is recorded at the start and end of every cycle for
 * the metrics analytics, and drives the per-thermostat cooling lockout
 * (Issue #209), which is why this lives on the Thermostats page.
 *
 * ``onChange`` fires with the configured entity id (or null) on load and on
 * every save, so a parent can react (e.g. enable/disable dependent settings).
 */
export default function OutsideTempPicker({
  onChange,
}: {
  onChange?: (entityId: string | null) => void;
}) {
  const { fmtTemp } = useUnit();
  const [current, setCurrent] = useState<{
    entity_id: string | null;
    current_value: number | null;
  } | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const refresh = async () => {
    try {
      const cur = await getOutsideTempEntity();
      setCurrent(cur);
      onChange?.(cur.entity_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    }
  };

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onSelect = async (entity_id: string | null) => {
    setSaving(true);
    setError("");
    try {
      const next = await setOutsideTempEntity(entity_id);
      setCurrent(next);
      onChange?.(next.entity_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      <div className="card-title">Outside temperature sensor</div>
      <div className="text-sm text-muted" style={{ marginBottom: "1rem" }}>
        One Home Assistant sensor (or weather entity) for the whole home — it is not tied to a
        specific thermostat. Plenum records its reading at the start and end of every cycle for
        the metrics analytics, and it is <strong>required</strong> for the cooling lockout safety
        feature below to take effect.
      </div>

      {error && (
        <div className="badge badge-red" style={{ marginBottom: ".75rem" }}>
          {error}
        </div>
      )}

      <div style={{ display: "flex", gap: ".75rem", alignItems: "center", flexWrap: "wrap" }}>
        <div style={{ flex: "1 1 320px", minWidth: 200 }}>
          <EntityPicker
            domain={["sensor", "weather"]}
            placeholder="Search sensor / weather entities…"
            onSelect={(id) => void onSelect(id)}
          />
        </div>

        {current?.entity_id ? (
          <>
            <span className="badge badge-blue">
              {current.entity_id}
              {current.current_value !== null && ` · ${fmtTemp(current.current_value)}`}
            </span>
            <button
              className="btn btn-secondary"
              onClick={() => void onSelect(null)}
              disabled={saving}
              type="button"
            >
              Clear
            </button>
          </>
        ) : (
          <span className="text-sm text-muted">— None configured —</span>
        )}
      </div>
    </div>
  );
}
