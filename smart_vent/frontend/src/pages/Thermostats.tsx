import { useEffect, useRef, useState } from "react";
import {
  getThermostats,
  createThermostat,
  updateThermostat,
  deleteThermostat,
  testVacationMode,
  revertVacationTest,
  getSensorStaleness,
  setSensorStaleness,
  getOutsideTempEntity,
  type ThermostatConfig,
} from "../api";
import EntityPicker from "../components/EntityPicker";
import AirflowConfigBanner from "../components/AirflowConfigBanner";
import OutsideTempPicker from "../components/OutsideTempPicker";
import { EcoWorkedExample } from "../components/EcoMode";
import { ECO_NUMERIC_FIELDS } from "../eco";
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
  {
    key: "unavailable_abort_after_min",
    label: "Abort when thermostat unavailable (min)",
    help: "If the thermostat entity is unavailable in Home Assistant for this long while a cycle is running, abort the cycle and re-open all vents — while it is unavailable the engine cannot supervise the cycle (no timeout, no closed-vent watchdog). Shorter outages are tolerated and the cycle resumes untouched. Recommended: 5 min. 0 = never abort (not recommended).",
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
  const [totalVentsCount, setTotalVentsCount] = useState("");
  const [hasBypassDamper, setHasBypassDamper] = useState(false);
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
    const parsedTotal = parseInt(totalVentsCount, 10);
    if (!Number.isFinite(parsedTotal) || parsedTotal < 1) {
      setError("Total vent count is required — a positive integer");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const tc = await createThermostat({
        thermostat_entity_id: entityId,
        name: name.trim(),
        total_vents_count: parsedTotal,
        has_bypass_damper: hasBypassDamper,
      });
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

        <div className="form-group">
          <label className="form-label" htmlFor="add-thermo-total-vents">
            Total vent count on this thermostat *
          </label>
          <input
            id="add-thermo-total-vents"
            className="form-control"
            type="number"
            min="1"
            step="1"
            placeholder="e.g. 12"
            value={totalVentsCount}
            onChange={(e) => setTotalVentsCount(e.target.value)}
          />
          <div className="form-hint">
            <strong>Count every register on this thermostat — smart vents AND passive ones</strong>,
            not only the smart ones. Plenum uses this to keep enough airflow open that closing vents
            never raises duct static pressure to a point where the furnace trips its high-limit, the
            blower strains, or the AC evaporator freezes.
          </div>
        </div>

        <div className="form-group">
          <label
            htmlFor="add-thermo-bypass-damper"
            style={{ display: "flex", alignItems: "center", gap: ".5rem", cursor: "pointer" }}
          >
            <input
              id="add-thermo-bypass-damper"
              type="checkbox"
              checked={hasBypassDamper}
              onChange={(e) => setHasBypassDamper(e.target.checked)}
            />
            <span>I have a bypass damper</span>
          </label>
          <div className="form-hint">
            A mechanical relief valve that opens when duct static pressure exceeds a setpoint. Most
            residential systems do <strong>not</strong> have one. If yours does, ticking this
            disables the airflow floor — the damper handles pressure relief.
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
  onSaved,
}: {
  config: ThermostatConfig;
  onDeleted: () => void;
  onSaved?: (updated: ThermostatConfig) => void;
}) {
  const { unitLabel, toDisplay, toDisplayDelta } = useUnit();
  // Form state holds temperatures in DISPLAY units (°C or °F as the user
  // sees them). The backend converts to storage (°F) on the write boundary
  // via _to_f / _delta_to_f. See CLAUDE.md "Temperature unit system".
  // Field names like `cooling_lockout_below_f` describe storage semantics;
  // the value here is whatever unit the user is currently typing in.
  const toDisplayForm = (cfg: ThermostatConfig): ThermostatConfig => ({
    ...cfg,
    default_temp: cfg.default_temp != null ? toDisplay(cfg.default_temp) : null,
    min_setpoint: toDisplay(cfg.min_setpoint),
    max_setpoint: toDisplay(cfg.max_setpoint),
    deadband: toDisplayDelta(cfg.deadband),
    overshoot_delta: toDisplayDelta(cfg.overshoot_delta),
    cooling_lockout_below_f:
      cfg.cooling_lockout_below_f != null ? toDisplay(cfg.cooling_lockout_below_f) : null,
    // Eco Mode (Issue #404). Outdoor thresholds / full-drift temps are absolute;
    // max-drift and hysteresis are deltas. Converted to display units on init;
    // submitted raw (the backend converts). eco_mode_enabled is a plain bool.
    eco_cooling_outdoor_threshold: toDisplay(cfg.eco_cooling_outdoor_threshold),
    eco_cooling_full_drift_temp: toDisplay(cfg.eco_cooling_full_drift_temp),
    eco_cooling_max_drift: toDisplayDelta(cfg.eco_cooling_max_drift),
    eco_heating_outdoor_threshold: toDisplay(cfg.eco_heating_outdoor_threshold),
    eco_heating_full_drift_temp: toDisplay(cfg.eco_heating_full_drift_temp),
    eco_heating_max_drift: toDisplayDelta(cfg.eco_heating_max_drift),
    eco_hysteresis_band: toDisplayDelta(cfg.eco_hysteresis_band),
  });
  const [form, setForm] = useState<ThermostatConfig>(() => toDisplayForm(config));
  // Re-derive form when config changes OR when the unit context updates
  // (App fetches /api/settings async on mount — if /api/thermostats wins
  // that race, this card mounts with the default F context and the initial
  // useState bakes °F values into a form that's about to be labeled °C).
  // Without this effect the form would render °F numbers under a °C label,
  // and any save would round-trip the wrong value (#231 follow-up).
  useEffect(() => {
    setForm(toDisplayForm(config));
    // toDisplayForm closes over toDisplay/toDisplayDelta; deps reflect that.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config, toDisplay, toDisplayDelta]);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const [testingVacation, setTestingVacation] = useState(false);
  const [vacationTestActive, setVacationTestActive] = useState(false);
  const [vacationTestResult, setVacationTestResult] = useState<string | null>(null);
  // Eco Mode (Issue #404) is outdoor-temperature-driven, so the toggle is only
  // available once an outside-temperature sensor is configured (set at the top
  // of this page). Tracked here to gate the checkbox + save.
  const [hasOutsideSensor, setHasOutsideSensor] = useState(false);
  useEffect(() => {
    let cancelled = false;
    getOutsideTempEntity()
      .then((r) => {
        if (!cancelled) setHasOutsideSensor(!!r?.entity_id);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  // Track the "Saved!" reset timer so it can be cancelled on unmount. Without
  // this, a save that completes just before the card unmounts (e.g. a test
  // tearing down jsdom) fires setSaved() after teardown — "window is not
  // defined" in CI. Clearing on unmount keeps the timer from outliving the
  // component.
  const savedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    return () => {
      if (savedTimerRef.current !== null) clearTimeout(savedTimerRef.current);
    };
  }, []);

  const save = async () => {
    if (!form.name.trim()) {
      setError("Friendly name is required");
      return;
    }
    if (form.min_setpoint >= form.max_setpoint) {
      setError("Min setpoint must be less than max setpoint");
      return;
    }
    // Eco Mode (Issue #404) is outdoor-temperature-driven — block enabling it
    // without a configured outside-temperature sensor.
    if (form.eco_mode_enabled && !hasOutsideSensor) {
      setError(
        "Eco Mode needs an outside-temperature sensor. Configure one at the top of this page " +
          "(e.g. via a PirateWeather sensor) before enabling Eco Mode."
      );
      return;
    }
    setSaving(true);
    setError("");
    try {
      const updated = await updateThermostat(config.thermostat_entity_id, form);
      // Refresh the parent's baseline (°F storage units, as returned by the API)
      // so a later form reset re-derives from the just-saved values — never the
      // stale page-load config that would silently regress the DB on re-save.
      // (Issue #293)
      onSaved?.(updated);
      setSaved(true);
      if (savedTimerRef.current !== null) clearTimeout(savedTimerRef.current);
      savedTimerRef.current = setTimeout(() => setSaved(false), 2000);
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

      {/* Identity fields — auto-fit so the pair stacks on narrow phones
          instead of squeezing side-by-side (#458). */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(min(220px, 100%), 1fr))",
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
          <label
            className="form-label"
            htmlFor={`thermo-${config.thermostat_entity_id}-default_temp`}
          >
            Default presence temp ({unitLabel})
          </label>
          <input
            id={`thermo-${config.thermostat_entity_id}-default_temp`}
            className="form-control"
            type="number"
            step="0.5"
            value={form.default_temp ?? ""}
            placeholder={`e.g. ${Math.round(toDisplay(72))}`}
            onChange={(e) =>
              setForm((f) => ({
                ...f,
                default_temp: e.target.value ? parseFloat(e.target.value) : null,
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
          // Temp fields in `form` are already in display units (see useState
          // above), so render directly. Backend converts on save.
          const displayVal = form[key] as number;
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
                  setForm((f) => ({ ...f, [key]: v }));
                }}
              />
              <div className="form-hint">{help}</div>
            </div>
          );
        })}
      </div>

      {/* Overflow conditioning during the minimum-runtime hold (Issue #237).
          Boolean — rendered outside the SAFETY_FIELDS loop, which only
          handles numeric inputs. Only meaningful when min_cycle_runtime_min
          > 0; the helper text says so. Disabled in vacation mode regardless. */}
      <div className="form-group" style={{ maxWidth: 480, marginTop: "1rem" }}>
        <label
          htmlFor={`thermo-${config.thermostat_entity_id}-overflow`}
          style={{
            display: "flex",
            alignItems: "center",
            gap: ".5rem",
            cursor: "pointer",
          }}
        >
          <input
            id={`thermo-${config.thermostat_entity_id}-overflow`}
            type="checkbox"
            checked={form.overflow_during_min_runtime ?? true}
            disabled={(form.min_cycle_runtime_min ?? 0) <= 0}
            onChange={(e) =>
              setForm((f) => ({ ...f, overflow_during_min_runtime: e.target.checked }))
            }
          />
          <span>Redirect surplus air to other rooms during the minimum-runtime hold</span>
        </label>
        <div className="form-hint">
          When a cycle hits its target before the minimum runtime has elapsed, the HVAC keeps
          running. With this on (default), the engine opens vents in non-active rooms that can
          absorb the surplus air without crossing into the opposite-direction trigger, so the
          satisfied rooms do not overshoot and the air handler has a larger plenum. Turn off if you
          would rather keep only the cycle's original rooms open during the hold. No effect unless{" "}
          <strong>Min cycle runtime</strong> above is &gt; 0; disabled automatically in vacation
          mode. See the{" "}
          <a
            href="https://github.com/dhruvb14/smart-thermostat-with-vents/blob/main/docs/overflow-conditioning.md"
            target="_blank"
            rel="noopener noreferrer"
          >
            overflow conditioning docs
          </a>{" "}
          for the tier-by-tier candidate selection.
        </div>
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
          value={form.cooling_lockout_below_f ?? ""}
          onChange={(e) => {
            const raw = e.target.value;
            setForm((f) => ({
              ...f,
              cooling_lockout_below_f: raw === "" ? null : parseFloat(raw) || 0,
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

      {/* Eco Mode — outdoor-temperature-compensated setpoint drift (Issue #404).
          Global per-thermostat; overridable per room on the Rooms page. Fields
          hold display units and submit the raw value (the backend converts). */}
      <hr className="divider" />
      <div
        className="text-sm"
        style={{ fontWeight: 600, color: "var(--gray-700)", marginBottom: ".5rem" }}
      >
        Eco Mode — outdoor-compensated setpoint drift
      </div>
      <div className="form-group" style={{ maxWidth: 560 }}>
        <label
          htmlFor={`thermo-${config.thermostat_entity_id}-eco-enabled`}
          style={{ display: "flex", alignItems: "center", gap: ".5rem", cursor: "pointer" }}
        >
          <input
            id={`thermo-${config.thermostat_entity_id}-eco-enabled`}
            type="checkbox"
            checked={form.eco_mode_enabled ?? false}
            disabled={!hasOutsideSensor && !form.eco_mode_enabled}
            onChange={(e) => setForm((f) => ({ ...f, eco_mode_enabled: e.target.checked }))}
          />
          <span>Enable Eco Mode for this thermostat</span>
        </label>
        {!hasOutsideSensor && (
          <div className="form-hint" style={{ color: "var(--orange)" }}>
            Eco Mode needs an outside-temperature sensor — configure one at the top of this page to
            enable it. No physical outdoor thermometer in Home Assistant? Add a free weather
            integration such as <strong>PirateWeather</strong> and point the outside-temperature
            setting at its temperature sensor.
          </div>
        )}
        <div className="form-hint">
          When it is extreme outside, relax each room&rsquo;s target toward a configured drift so
          the HVAC works less. Off by default; a room can still opt in individually. Requires the
          outside-temperature sensor configured at the top of this page.
        </div>
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(230px, 1fr))",
          gap: "1rem",
        }}
      >
        {ECO_NUMERIC_FIELDS.map(({ key, label, help, step, kind }) => {
          const fieldLabel = `${label} (${unitLabel})`;
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
                value={(form[key] as number) ?? ""}
                onChange={(e) => {
                  const v = parseFloat(e.target.value) || 0;
                  setForm((f) => ({ ...f, [key]: v }));
                }}
              />
              <div className="form-hint">
                {kind === "absolute_temp" ? "Outdoor temperature. " : "Difference. "}
                {help}
              </div>
            </div>
          );
        })}
      </div>
      <div style={{ marginTop: ".75rem", maxWidth: 560 }}>
        <EcoWorkedExample
          params={{
            coolingThreshold: form.eco_cooling_outdoor_threshold,
            coolingFullDrift: form.eco_cooling_full_drift_temp,
            coolingMaxDrift: form.eco_cooling_max_drift,
            heatingThreshold: form.eco_heating_outdoor_threshold,
            heatingFullDrift: form.eco_heating_full_drift_temp,
            heatingMaxDrift: form.eco_heating_max_drift,
          }}
        />
      </div>

      {/* Airflow floor / dead-head protection (Issue #213). Replaces the
          legacy ``min_open_vents`` count with a fraction of total registers
          (smart + passive). */}
      <hr className="divider" />
      <div
        className="text-sm"
        style={{ fontWeight: 600, color: "var(--gray-700)", marginBottom: ".75rem" }}
      >
        Airflow floor — dead-head protection
      </div>
      <div className="form-hint" style={{ marginBottom: "1rem" }}>
        Closing too many vents at once raises duct static pressure and can trip a furnace
        high-limit, strain the blower, or freeze the evaporator coil. The floor keeps a fraction of
        the thermostat's total registers open at all times.
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
          gap: "1rem",
        }}
      >
        <div className="form-group" style={{ marginBottom: 0 }}>
          <label
            className="form-label"
            htmlFor={`thermo-${config.thermostat_entity_id}-total-vents`}
          >
            Total vent count
          </label>
          <input
            id={`thermo-${config.thermostat_entity_id}-total-vents`}
            className="form-control"
            type="number"
            min="1"
            step="1"
            placeholder="e.g. 12"
            value={form.total_vents_count ?? ""}
            onChange={(e) => {
              const raw = e.target.value;
              setForm((f) => ({
                ...f,
                total_vents_count: raw === "" ? null : Math.max(1, parseInt(raw, 10) || 0),
              }));
            }}
          />
          <div className="form-hint">
            <strong>Every register on this thermostat — smart vents AND passive ones.</strong>{" "}
            Passive registers are always open and reduce how many of the smart vents have to stay
            open.
          </div>
        </div>

        <div className="form-group" style={{ marginBottom: 0 }}>
          <label
            htmlFor={`thermo-${config.thermostat_entity_id}-bypass-damper`}
            style={{
              display: "flex",
              alignItems: "center",
              gap: ".5rem",
              cursor: "pointer",
              marginTop: "1.5rem",
            }}
          >
            <input
              id={`thermo-${config.thermostat_entity_id}-bypass-damper`}
              type="checkbox"
              checked={form.has_bypass_damper}
              onChange={(e) => setForm((f) => ({ ...f, has_bypass_damper: e.target.checked }))}
            />
            <span>I have a bypass damper</span>
          </label>
          <div className="form-hint">
            A mechanical relief valve that opens when duct pressure exceeds a setpoint. Most homes
            don't have one. Ticking this disables the airflow floor.
          </div>
        </div>
      </div>

      <div className="form-group" style={{ marginTop: "1rem", marginBottom: 0 }}>
        <label className="form-label" htmlFor={`thermo-${config.thermostat_entity_id}-fraction`}>
          Minimum open fraction:{" "}
          <strong>{Math.round((form.min_open_vents_fraction ?? 0.333) * 100)}%</strong> of total
          vents
        </label>
        <input
          id={`thermo-${config.thermostat_entity_id}-fraction`}
          type="range"
          min={0.1}
          max={1}
          step={0.05}
          value={form.min_open_vents_fraction ?? 0.333}
          disabled={form.has_bypass_damper}
          onChange={(e) =>
            setForm((f) => ({ ...f, min_open_vents_fraction: parseFloat(e.target.value) }))
          }
          style={{ width: "100%", maxWidth: 320 }}
        />
        <div className="form-hint">
          {form.has_bypass_damper ? (
            <em>Not enforced — your bypass damper handles pressure relief.</em>
          ) : (
            <>
              Default 33% (one third). Raise it for tighter safety, lower it if your duct system can
              tolerate more closed vents.
            </>
          )}
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
                {form.min_setpoint}
                {unitLabel}
              </strong>{" "}
              and an upper bound of{" "}
              <strong>
                {form.max_setpoint}
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
                {form.min_setpoint}
                {unitLabel}
              </strong>{" "}
              it switches to heat mode; if it rises above{" "}
              <strong>
                {form.max_setpoint}
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
// Sensor-staleness threshold (Issue #211)
// ---------------------------------------------------------------------------

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
        <label htmlFor="thermostats-sensor-stale-min" className="form-label" style={{ margin: 0 }}>
          Minutes
        </label>
        <input
          id="thermostats-sensor-stale-min"
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

      <AirflowConfigBanner />

      <OutsideTempPicker />

      <SensorStalenessCard />

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
          <ThermostatCard
            key={c.thermostat_entity_id}
            config={c}
            onDeleted={load}
            onSaved={(updated) =>
              setConfigs((cs) =>
                cs.map((x) =>
                  x.thermostat_entity_id === updated.thermostat_entity_id ? updated : x
                )
              )
            }
          />
        ))
      )}

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
