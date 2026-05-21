import { useEffect, useRef, useState } from "react";
import {
  getThermostats,
  createThermostat,
  updateThermostat,
  deleteThermostat,
  downloadBackup,
  restoreBackup,
  testVacationMode,
  revertVacationTest,
  type ThermostatConfig,
} from "../api";
import EntityPicker from "../components/EntityPicker";
import OutsideTempPicker from "../components/OutsideTempPicker";
import { useUnit } from "../contexts";

// ---------------------------------------------------------------------------
// Safety settings fields (numerical config)
// ---------------------------------------------------------------------------

const SAFETY_FIELDS: {
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
    help: "±tolerance to consider a room 'at target'. 0 = exact match.",
    step: "0.1",
    min: "0",
    kind: "delta_temp",
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
    key: "max_vent_closed_min",
    label: "Max vent closed (min)",
    help: "Reopen vents after this many minutes. 0 = disabled.",
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
    key: "cycle_timeout_hours",
    label: "Cycle timeout (hours)",
    help: "Abort a stuck cycle after this many hours",
    step: "0.5",
    min: "0.5",
    kind: "other",
  },
  {
    key: "min_cycle_runtime_min",
    label: "Min cycle runtime (min)",
    help: "Keep the HVAC running at least this long before completing a cycle, to protect the compressor from short-cycling. Recommended: 10 min (long enough to return oil to the compressor and condition effectively). 0 = disabled.",
    step: "1",
    min: "0",
    kind: "other",
  },
  {
    key: "min_cycle_offtime_min",
    label: "Min compressor off-time (min)",
    help: "Wait at least this long after a cycle ends before starting a new one, so the compressor is not restarted too soon. Recommended: 5 min — the industry-standard anti-short-cycle delay that lets refrigerant pressures equalize. 0 = disabled.",
    step: "1",
    min: "0",
    kind: "other",
  },
];

// ---------------------------------------------------------------------------
// Add thermostat modal
// ---------------------------------------------------------------------------

function AddThermostatModal({
  onClose,
  onSave,
}: {
  onClose: () => void;
  onSave: (tc: ThermostatConfig) => void;
}) {
  const [entityId, setEntityId] = useState("");
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const save = async () => {
    if (!entityId) {
      setError("Select a thermostat entity");
      return;
    }
    if (!name.trim()) {
      setError("Friendly name is required");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const tc = await createThermostat({ thermostat_entity_id: entityId, name: name.trim() });
      onSave(tc);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-title">Register Thermostat</div>
        {error && (
          <div className="badge badge-red" style={{ marginBottom: "1rem" }}>
            {error}
          </div>
        )}

        <div className="form-group">
          <label className="form-label">HA Thermostat Entity *</label>
          <EntityPicker domain="climate" placeholder="Search thermostats…" onSelect={setEntityId} />
          {entityId && (
            <div className="tag" style={{ marginTop: ".4rem" }}>
              ✓ <span className="font-mono">{entityId}</span>
              <button className="tag-remove" onClick={() => setEntityId("")}>
                ×
              </button>
            </div>
          )}
        </div>

        <div className="form-group">
          <label className="form-label" htmlFor="add-thermo-name">
            Friendly name *
          </label>
          <input
            id="add-thermo-name"
            className="form-control"
            placeholder="e.g. Upstairs HVAC"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
          />
          <div className="form-hint">
            This name appears on the Dashboard and in Room configuration.
          </div>
        </div>

        <div className="modal-footer">
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Register"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-thermostat card (name, default_temp, safety settings)
// ---------------------------------------------------------------------------

function ThermostatCard({
  config,
  onDeleted,
}: {
  config: ThermostatConfig;
  onDeleted: () => void;
}) {
  const { unitLabel, toDisplay, toDisplayDelta, toStorage, toStorageDelta } = useUnit();
  const [form, setForm] = useState({ ...config });
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const [testingVacation, setTestingVacation] = useState(false);
  const [vacationTestActive, setVacationTestActive] = useState(false);
  const [vacationTestResult, setVacationTestResult] = useState<string | null>(null);

  const save = async () => {
    if (!form.name.trim()) {
      setError("Friendly name is required");
      return;
    }
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

  const remove = async () => {
    if (
      !confirm(
        `Remove thermostat "${form.name || config.thermostat_entity_id}"?\n\nRooms already using this thermostat will keep their entity ID but the thermostat will no longer appear in the picker.`
      )
    )
      return;
    try {
      await deleteThermostat(config.thermostat_entity_id);
      onDeleted();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Delete failed");
    }
  };

  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      {/* Header */}
      <div className="flex-between" style={{ marginBottom: "1rem" }}>
        <div>
          <div className="card-title" style={{ marginBottom: ".15rem" }}>
            {form.name || <span className="text-muted">(unnamed)</span>}
          </div>
          <div className="font-mono text-muted" style={{ fontSize: ".78rem" }}>
            {config.thermostat_entity_id}
          </div>
        </div>
        <button className="btn btn-danger btn-sm" onClick={remove}>
          Remove
        </button>
      </div>

      {error && (
        <div className="badge badge-red" style={{ marginBottom: "1rem" }}>
          {error}
        </div>
      )}

      {/* Identity fields */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: "1rem",
          marginBottom: "1rem",
        }}
      >
        <div className="form-group" style={{ marginBottom: 0 }}>
          <label className="form-label" htmlFor={`thermo-${config.thermostat_entity_id}-name`}>
            Friendly name *
          </label>
          <input
            id={`thermo-${config.thermostat_entity_id}-name`}
            className="form-control"
            value={form.name}
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            placeholder="e.g. Upstairs HVAC"
          />
        </div>
        <div className="form-group" style={{ marginBottom: 0 }}>
          <label className="form-label">Default presence temp ({unitLabel})</label>
          <input
            className="form-control"
            type="number"
            step="0.5"
            value={form.default_temp != null ? toDisplay(form.default_temp) : ""}
            placeholder={`e.g. ${Math.round(toDisplay(72))}`}
            onChange={(e) =>
              setForm((f) => ({
                ...f,
                default_temp: e.target.value ? toStorage(parseFloat(e.target.value)) : null,
              }))
            }
          />
          <div className="form-hint">
            Target temperature when a room in this zone is activated by presence and has no
            room-level presence temp set. Rooms can override this individually.
          </div>
        </div>
      </div>

      <hr className="divider" />

      {/* Safety settings */}
      <div
        className="text-sm"
        style={{ fontWeight: 600, color: "var(--gray-700)", marginBottom: ".75rem" }}
      >
        Safety &amp; cycle settings
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))",
          gap: "1rem",
        }}
      >
        {SAFETY_FIELDS.map(({ key, label, help, step, min, kind }) => {
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
              <label
                className="form-label"
                htmlFor={`thermo-${config.thermostat_entity_id}-${key}`}
              >
                {fieldLabel}
              </label>
              <input
                id={`thermo-${config.thermostat_entity_id}-${key}`}
                className="form-control"
                type="number"
                step={step}
                min={min}
                value={displayVal ?? ""}
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
              <div className="form-hint">{help}</div>
            </div>
          );
        })}
      </div>

      {/* Outdoor-temperature cooling lockout (Issue #209). Nullable (blank =
          disabled), so it is rendered outside the SAFETY_FIELDS loop. */}
      <div className="form-group" style={{ maxWidth: 280, marginTop: "1rem" }}>
        <label
          className="form-label"
          htmlFor={`thermo-${config.thermostat_entity_id}-cooling-lockout`}
        >
          Cooling lockout — pause AC below ({unitLabel})
        </label>
        <input
          id={`thermo-${config.thermostat_entity_id}-cooling-lockout`}
          className="form-control"
          type="number"
          step="0.5"
          placeholder="Disabled"
          value={
            form.cooling_lockout_below_f != null ? toDisplay(form.cooling_lockout_below_f) : ""
          }
          onChange={(e) => {
            const raw = e.target.value;
            setForm((f) => ({
              ...f,
              cooling_lockout_below_f: raw === "" ? null : toStorage(parseFloat(raw) || 0),
            }));
          }}
        />
        <div className="form-hint">
          Skip cooling cycles while the outdoor temperature is below this, to protect the AC
          compressor from cold-weather operation (liquid slugging, evaporator coil icing).
          Recommended around 55°F (about 13°C). Leave blank to disable. Requires the
          outside-temperature sensor (configured at the top of this page). Heat pumps are not
          supported.
        </div>
      </div>

      <hr className="divider" />

      {/* Drift correction — rendered separately because max depends on cycle_timeout_hours */}
      <div
        className="text-sm"
        style={{ fontWeight: 600, color: "var(--gray-700)", marginBottom: ".75rem" }}
      >
        Drift correction
      </div>
      <div className="form-group" style={{ maxWidth: 280 }}>
        <label className="form-label" htmlFor={`thermo-${config.thermostat_entity_id}-drift`}>
          Drift correction interval (min)
        </label>
        <input
          id={`thermo-${config.thermostat_entity_id}-drift`}
          className="form-control"
          type="number"
          step="1"
          min="0"
          max={Math.floor(form.cycle_timeout_hours * 60)}
          value={form.reconciliation_interval_min ?? 0}
          onChange={(e) => {
            const val = parseInt(e.target.value) || 0;
            const maxVal = Math.floor(form.cycle_timeout_hours * 60);
            setForm((f) => ({ ...f, reconciliation_interval_min: Math.min(val, maxVal) }));
          }}
        />
        <div className="form-hint">
          How often (in minutes) the engine re-checks actual vent and thermostat state in Home
          Assistant and corrects any external changes (e.g. from other integrations or manual HA
          overrides). Set to <strong>0</strong> to disable. Cannot exceed the cycle timeout (
          {Math.floor(form.cycle_timeout_hours * 60)} min).
        </div>
      </div>

      <hr className="divider" />

      {/* Vacation mode HVAC strategy */}
      <div
        className="text-sm"
        style={{ fontWeight: 600, color: "var(--gray-700)", marginBottom: ".75rem" }}
      >
        Vacation mode hold strategy
      </div>
      <div className="form-hint" style={{ marginBottom: ".75rem" }}>
        Vacation mode is enabled system-wide from the Dashboard and applies to all thermostats.
        Configure below how <em>this</em> thermostat should hold temperature while vacation mode is
        active.
      </div>

      <div className="form-group" style={{ maxWidth: 400 }}>
        <label
          className="form-label"
          htmlFor={`thermo-${config.thermostat_entity_id}-vacation-mode`}
        >
          Vacation HVAC mode
        </label>
        <select
          id={`thermo-${config.thermostat_entity_id}-vacation-mode`}
          className="form-control"
          value={form.vacation_hvac_mode ?? "single"}
          onChange={(e) =>
            setForm((f) => ({
              ...f,
              vacation_hvac_mode: e.target.value as "range" | "single",
              // Clear test result when switching mode
            }))
          }
        >
          <option value="single">Single setpoint (heat or cool)</option>
          <option value="range">Range (heat_cool / auto)</option>
        </select>
        <div className="form-hint">
          {form.vacation_hvac_mode === "range" ? (
            <>
              Requires your thermostat to support <strong>heat_cool</strong> or{" "}
              <strong>auto</strong> mode in Home Assistant. During vacation mode the thermostat will
              be set to <em>heat_cool</em> with a lower bound of{" "}
              <strong>
                {toDisplay(form.min_setpoint)}
                {unitLabel}
              </strong>{" "}
              and an upper bound of{" "}
              <strong>
                {toDisplay(form.max_setpoint)}
                {unitLabel}
              </strong>
              , letting it manage both heating and cooling natively.
            </>
          ) : (
            <>
              For thermostats that only support a single target temperature at a time. During
              vacation mode the system turns the HVAC <strong>off</strong>. If the temperature drops
              below{" "}
              <strong>
                {toDisplay(form.min_setpoint)}
                {unitLabel}
              </strong>{" "}
              it switches to heat mode; if it rises above{" "}
              <strong>
                {toDisplay(form.max_setpoint)}
                {unitLabel}
              </strong>{" "}
              it switches to cool mode. Once back in range, the HVAC turns off again.
            </>
          )}
        </div>
      </div>

      {form.vacation_hvac_mode === "range" && (
        <div className="form-group" style={{ maxWidth: 400 }}>
          <div style={{ display: "flex", alignItems: "center", gap: ".75rem", flexWrap: "wrap" }}>
            {!vacationTestActive ? (
              <button
                className="btn btn-secondary"
                disabled={testingVacation}
                onClick={async () => {
                  setTestingVacation(true);
                  setVacationTestResult(null);
                  try {
                    await testVacationMode(config.thermostat_entity_id);
                    setVacationTestActive(true);
                    setVacationTestResult(
                      "heat_cool active — check your thermostat in Home Assistant."
                    );
                  } catch (e: unknown) {
                    setVacationTestResult(
                      "Error: " + (e instanceof Error ? e.message : "Test failed")
                    );
                  } finally {
                    setTestingVacation(false);
                  }
                }}
              >
                {testingVacation ? "Testing…" : "Test auto mode"}
              </button>
            ) : (
              <button
                className="btn btn-warning"
                onClick={async () => {
                  try {
                    await revertVacationTest(config.thermostat_entity_id);
                    setVacationTestActive(false);
                    setVacationTestResult("Reverted — thermostat set back to off.");
                  } catch (e: unknown) {
                    setVacationTestResult(
                      "Error: " + (e instanceof Error ? e.message : "Revert failed")
                    );
                  }
                }}
              >
                Revert test
              </button>
            )}
            {vacationTestResult && (
              <span
                className={`badge ${vacationTestResult.startsWith("Error") ? "badge-red" : "badge-green"}`}
              >
                {vacationTestResult}
              </span>
            )}
          </div>
          <div className="form-hint" style={{ marginTop: ".5rem" }}>
            <em>Test auto mode</em> sends the <strong>heat_cool</strong> command with your current
            min/max setpoints so you can verify the thermostat responded in Home Assistant. Click{" "}
            <em>Revert test</em> to set it back to off immediately, or the engine will revert it
            automatically on the next tick (~60 s).
          </div>
        </div>
      )}

      <div style={{ marginTop: "1.25rem", display: "flex", alignItems: "center", gap: ".75rem" }}>
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save changes"}
        </button>
        {saved && <span className="badge badge-green">Saved!</span>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Backup / Restore card
// ---------------------------------------------------------------------------

function BackupRestoreCard() {
  const fileRef = useRef<HTMLInputElement>(null);
  const [restoring, setRestoring] = useState(false);
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);

  const handleRestore = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (
      !confirm("Restore this database? Current data will be replaced and the engine will restart.")
    ) {
      e.target.value = "";
      return;
    }
    setRestoring(true);
    setStatus(null);
    try {
      await restoreBackup(file);
      setStatus({ ok: true, msg: "Restore complete — configuration reloaded." });
    } catch (err) {
      setStatus({ ok: false, msg: err instanceof Error ? err.message : "Restore failed" });
    } finally {
      setRestoring(false);
      e.target.value = "";
    }
  };

  return (
    <div className="card" style={{ marginTop: "2rem" }}>
      <div className="card-title" style={{ marginBottom: ".25rem" }}>
        Backup &amp; Restore
      </div>
      <div className="text-muted" style={{ fontSize: ".85rem", marginBottom: "1.25rem" }}>
        Download your configuration database or restore from a previous backup.
      </div>
      <div style={{ display: "flex", gap: ".75rem", alignItems: "center", flexWrap: "wrap" }}>
        <button className="btn btn-secondary" onClick={downloadBackup}>
          Download backup
        </button>
        <button
          className="btn btn-secondary"
          onClick={() => fileRef.current?.click()}
          disabled={restoring}
        >
          {restoring ? "Restoring…" : "Restore from backup"}
        </button>
        <input
          ref={fileRef}
          id="restore-backup-input"
          type="file"
          accept=".db"
          style={{ display: "none" }}
          onChange={handleRestore}
        />
        {status && (
          <span className={`badge ${status.ok ? "badge-green" : "badge-red"}`}>{status.msg}</span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function Thermostats() {
  const [configs, setConfigs] = useState<ThermostatConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);

  const load = async () => {
    const tc = await getThermostats();
    setConfigs(tc);
    setLoading(false);
  };

  useEffect(() => {
    load();
  }, []);

  if (loading)
    return (
      <div className="loading">
        <div className="spinner" /> Loading thermostats…
      </div>
    );

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">Thermostats</div>
          <div className="page-subtitle">
            Register your thermostats here first, then assign rooms to them
          </div>
        </div>
        <button className="btn btn-primary" onClick={() => setShowAdd(true)}>
          + Register thermostat
        </button>
      </div>

      <OutsideTempPicker />

      {configs.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <p>No thermostats registered yet.</p>
            <p style={{ marginTop: ".5rem" }}>
              Click <strong>+ Register thermostat</strong> to add your first thermostat. Once
              registered, it will appear in the Room configuration picker by its friendly name.
            </p>
          </div>
        </div>
      ) : (
        configs.map((c) => (
          <ThermostatCard key={c.thermostat_entity_id} config={c} onDeleted={load} />
        ))
      )}

      <BackupRestoreCard />

      {showAdd && (
        <AddThermostatModal
          onClose={() => setShowAdd(false)}
          onSave={() => {
            setShowAdd(false);
            load();
          }}
        />
      )}
    </div>
  );
}
