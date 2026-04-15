import { useEffect, useState } from "react";
import { getThermostats, updateThermostat, getRooms, type ThermostatConfig } from "../api";

const FIELDS: {
  key: keyof ThermostatConfig;
  label: string;
  help: string;
  step: string;
  min: string;
}[] = [
  {
    key: "min_setpoint",
    label: "Min setpoint (°F)",
    help: "Never set thermostat below this temperature",
    step: "0.5",
    min: "40",
  },
  {
    key: "max_setpoint",
    label: "Max setpoint (°F)",
    help: "Never set thermostat above this temperature",
    step: "0.5",
    min: "40",
  },
  {
    key: "deadband",
    label: "Deadband (°F)",
    help: "±tolerance to consider a room 'at target'. 0 = exact match. Prevents rapid cycling.",
    step: "0.1",
    min: "0",
  },
  {
    key: "max_vent_closed_min",
    label: "Max vent closed (min)",
    help: "Reopen vents after this many minutes. 0 = disabled (use for bypass damper systems).",
    step: "1",
    min: "0",
  },
  {
    key: "min_open_vents",
    label: "Min open vents",
    help: "Always keep at least this many vents open. 0 = allow all closed.",
    step: "1",
    min: "0",
  },
  {
    key: "overshoot_delta",
    label: "Overshoot delta (°F)",
    help: "How far past target to set the thermostat to keep the HVAC running",
    step: "0.5",
    min: "0",
  },
  {
    key: "cycle_timeout_hours",
    label: "Cycle timeout (hours)",
    help: "Abort a stuck cycle after this many hours",
    step: "0.5",
    min: "0.5",
  },
];

function ThermostatCard({ config }: { config: ThermostatConfig }) {
  const [form, setForm] = useState<ThermostatConfig>({ ...config });
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  const save = async () => {
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
        {FIELDS.map(({ key, label, help, step, min }) => (
          <div className="form-group" key={key} style={{ marginBottom: 0 }}>
            <label className="form-label">{label}</label>
            <input
              className="form-control"
              type="number"
              step={step}
              min={min}
              value={form[key] as number}
              onChange={(e) => setForm((f) => ({ ...f, [key]: parseFloat(e.target.value) || 0 }))}
            />
            <div className="text-sm text-muted" style={{ marginTop: ".25rem" }}>
              {help}
            </div>
          </div>
        ))}
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

      {configs.map((c) => (
        <ThermostatCard key={c.thermostat_entity_id} config={c} />
      ))}
    </div>
  );
}
