import { useEffect, useState } from "react";
import {
  getThermostats,
  updateThermostat,
  getRooms,
  getSensorStaleness,
  setSensorStaleness,
  type ThermostatConfig,
} from "../api";
import { useUnit } from "../contexts";

function SensorStalenessCard() {
  const [value, setValue] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);

  useEffect(() => {
    getSensorStaleness()
      .then((s) => setValue(s.stale_after_min))
      .catch(() => setValue(30));
  }, []);

  const save = async () => {
    if (value === null) return;
    setSaving(true);
    setStatus(null);
    try {
      const r = await setSensorStaleness(value);
      setValue(r.stale_after_min);
      setStatus({ ok: true, msg: "Saved" });
    } catch (err) {
      setStatus({ ok: false, msg: (err as Error).message });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      <div className="card-title">Sensor-staleness threshold</div>
      <div className="form-hint" style={{ marginBottom: ".75rem" }}>
        Temperature sensor readings older than this are excluded from the room temperature average
        so the engine never drives control decisions off stale data (Issue&nbsp;#211).
        Battery-powered Zigbee/Z-Wave sensors that drop off the mesh keep showing their last numeric
        value in Home Assistant — without this guard, that stale value would silently poison the
        average. Typical: <strong>30 min</strong>. Increase if your sensors report infrequently;
        decrease if you want a tighter check.
      </div>
      <div style={{ display: "flex", gap: ".5rem", alignItems: "center" }}>
        <label htmlFor="settings-sensor-stale-min" className="form-label" style={{ margin: 0 }}>
          Minutes
        </label>
        <input
          id="settings-sensor-stale-min"
          className="form-control"
          type="number"
          min={1}
          max={24 * 60}
          step={1}
          value={value ?? ""}
          onChange={(e) => setValue(parseFloat(e.target.value) || 0)}
          style={{ width: 120 }}
        />
        <button className="btn btn-primary" onClick={() => void save()} disabled={saving}>
          Save
        </button>
        {status && (
          <span
            className={status.ok ? "text-success" : "text-danger"}
            style={{ marginLeft: ".5rem" }}
          >
            {status.msg}
          </span>
        )}
      </div>
    </div>
  );
}

const FIELDS: {
  key: keyof ThermostatConfig;
  label: string;
  help: string;
  step: string;
  min: string;
  kind: "absolute_temp" | "delta_temp" | "other";
}[] = [
  {
    key: "min_setpoint",
    label: "Min setpoint",
    help: "Never set thermostat below this temperature",
    step: "0.5",
    min: "0",
    kind: "absolute_temp",
  },
  {
    key: "max_setpoint",
    label: "Max setpoint",
    help: "Never set thermostat above this temperature",
    step: "0.5",
    min: "0",
    kind: "absolute_temp",
  },
  {
    key: "deadband",
    label: "Deadband",
    help: "±tolerance to consider a room 'at target'. 0 = exact match. Prevents rapid cycling.",
    step: "0.1",
    min: "0",
    kind: "delta_temp",
  },
  {
    key: "max_vent_closed_min",
    label: "Max vent closed (min)",
    help: "Reopen vents after this many minutes. 0 = disabled (use for bypass damper systems).",
    step: "1",
    min: "0",
    kind: "other",
  },
  {
    key: "min_open_vents",
    label: "Min open vents",
    help: "Always keep at least this many vents open. 0 = allow all closed.",
    step: "1",
    min: "0",
    kind: "other",
  },
  {
    key: "overshoot_delta",
    label: "Overshoot delta",
    help: "How far past target to set the thermostat to keep the HVAC running",
    step: "0.5",
    min: "0",
    kind: "delta_temp",
  },
  {
    key: "cycle_timeout_hours",
    label: "Cycle timeout (hours)",
    help: "Abort a stuck cycle after this many hours",
    step: "0.5",
    min: "0.5",
    kind: "other",
  },
];

function ThermostatCard({ config }: { config: ThermostatConfig }) {
  const { unitLabel, toDisplay, toDisplayDelta, toStorage, toStorageDelta } = useUnit();
  const [form, setForm] = useState<ThermostatConfig>({ ...config });
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  const save = async () => {
    if (form.min_setpoint >= form.max_setpoint) {
      setError("Min setpoint must be less than max setpoint");
      return;
    }
    setSaving(true);
    setError("");
    try {
      await updateThermostat(config.thermostat_entity_id, form);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      <div className="card-title">{config.thermostat_entity_id}</div>
      {error && (
        <div className="badge badge-red" style={{ marginBottom: "1rem" }}>
          {error}
        </div>
      )}

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
          gap: "1rem",
        }}
      >
        {FIELDS.map(({ key, label, help, step, min, kind }) => {
          const rawVal = form[key] as number;
          const displayVal =
            kind === "absolute_temp"
              ? toDisplay(rawVal)
              : kind === "delta_temp"
                ? toDisplayDelta(rawVal)
                : rawVal;
          const fieldLabel =
            kind === "absolute_temp" || kind === "delta_temp" ? `${label} (${unitLabel})` : label;
          return (
            <div className="form-group" key={key} style={{ marginBottom: 0 }}>
              <label className="form-label" htmlFor={`settings-${key}`}>
                {fieldLabel}
              </label>
              <input
                id={`settings-${key}`}
                className="form-control"
                type="number"
                step={step}
                min={min}
                value={displayVal}
                onChange={(e) => {
                  const v = parseFloat(e.target.value) || 0;
                  const stored =
                    kind === "absolute_temp"
                      ? toStorage(v)
                      : kind === "delta_temp"
                        ? toStorageDelta(v)
                        : v;
                  setForm((f) => ({ ...f, [key]: stored }));
                }}
              />
              <div className="text-sm text-muted" style={{ marginTop: ".25rem" }}>
                {help}
              </div>
            </div>
          );
        })}
      </div>

      <div style={{ marginTop: "1.25rem", display: "flex", alignItems: "center", gap: ".75rem" }}>
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save changes"}
        </button>
        {saved && <span className="badge badge-green">Saved!</span>}
      </div>
    </div>
  );
}

export default function Settings() {
  const [configs, setConfigs] = useState<ThermostatConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [noRooms, setNoRooms] = useState(false);

  useEffect(() => {
    Promise.all([getThermostats(), getRooms()]).then(([tc, rooms]) => {
      // Also create default configs for thermostats referenced by rooms but not yet configured
      const knownIds = new Set(tc.map((c) => c.thermostat_entity_id));
      const roomThermoIds = [...new Set(rooms.map((r) => r.thermostat_entity_id))];
      const missing = roomThermoIds.filter((id) => !knownIds.has(id));
      // Trigger save of defaults for missing ones
      Promise.all(missing.map((id) => updateThermostat(id, {}))).then(() => {
        getThermostats().then((updated) => {
          setConfigs(updated);
          setLoading(false);
        });
      });
      if (rooms.length === 0) setNoRooms(true);
      setConfigs(tc);
      setLoading(false);
    });
  }, []);

  if (loading)
    return (
      <div className="loading">
        <div className="spinner" /> Loading settings…
      </div>
    );

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">Settings</div>
          <div className="page-subtitle">Per-thermostat safety limits and cycle configuration</div>
        </div>
      </div>

      {noRooms && configs.length === 0 && (
        <div className="card">
          <div className="empty-state">
            <p>
              No thermostats configured yet. Add rooms first — thermostat configs will appear here.
            </p>
          </div>
        </div>
      )}

      <SensorStalenessCard />

      {configs.map((c) => (
        <ThermostatCard key={c.thermostat_entity_id} config={c} />
      ))}
    </div>
  );
}
