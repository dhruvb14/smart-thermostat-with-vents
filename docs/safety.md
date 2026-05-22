# Safety features

Plenum drives real HVAC equipment, so several settings exist purely to protect that equipment from being damaged by software decisions. This page collects them and is updated as new protections are added. Each is configured per-thermostat on the **Thermostats** page unless noted.

> **Heat pumps are not supported.** Plenum assumes a conventional system — a furnace or air handler for heat and an AC compressor for cooling. The cooling lockout below has no heating equivalent for that reason.

## Short-cycle protection

Rapidly stopping and restarting a compressor — *short-cycling* — is one of the most common ways control software damages HVAC equipment: it stresses the motor windings and contactor and prevents oil from returning to the compressor. Plenum bounds how often a cycle can stop and start.

| Setting | Default | What it does |
|---|---|---|
| **Min cycle runtime** | 0 (disabled) | Once a cycle starts, it is held open at least this many minutes before it may complete normally. Recommended **10 min**. |
| **Min compressor off-time** | 0 (disabled) | After a cycle ends, a new one may not start for this many minutes. Recommended **5 min** — the industry-standard anti-short-cycle delay that lets refrigerant pressures equalize. |

### How the minimum-runtime hold works

When every room in a cycle reaches its target before the minimum runtime has elapsed, Plenum does **not** stop the HVAC. It re-opens the vents of every room that was part of the cycle so the air handler keeps a full duct path — never dead-heading airflow through a single room — and the small unavoidable overshoot is spread evenly. The cycle completes normally once the runtime clock is satisfied.

### Off-time lockout

While the off-time lockout is active, the engine refuses to start a new cycle and writes a warning to the event log noting how long remains. An already-running cycle is never interrupted by the lockout.

### Existing thermostats

When you upgrade to a build that includes short-cycle protection, thermostats that already existed are **back-filled** with the recommended values (10 min runtime, 5 min off-time) — they are presumably controlling live equipment and should not be left unprotected. Thermostats registered afterwards start disabled (0) and you opt in from the UI. A thermostat you have already tuned by hand is left untouched.

## Outdoor-temperature cooling lockout

Running a standard AC compressor when it is cold outside risks liquid refrigerant slugging the compressor and ice forming on the evaporator coil. The cooling lockout suppresses cooling cycles in cold weather.

| Setting | Default | What it does |
|---|---|---|
| **Cooling lockout — pause AC below** | blank (disabled) | When set, the engine will not start a cooling cycle while the outdoor temperature is below this value. Recommended around **55 °F** for a conventional AC. Leave blank to disable. |

When a cooling cycle is suppressed, the thermostat is left idle and a warning is written to the event log explaining why, so the decision is auditable.

### Requires an outdoor sensor

The lockout needs to know the outdoor temperature. The **outside-temperature sensor** is configured once for the whole home, at the top of the **Thermostats** page — it is a single Home Assistant `sensor.*` or `weather.*` entity and is not tied to any individual thermostat. Without it, the lockout cannot evaluate.

### Fail-open behavior

If a lockout threshold is configured but the outdoor sensor is unset or unreadable, Plenum **fails open** — it allows the cooling cycle and logs a warning. A dropped sensor should not silently disable cooling for the whole house in summer; the warning makes the gap visible so it can be fixed.

## Sensor-staleness guard

A battery-powered Zigbee or Z-Wave temperature sensor that drops off the mesh does not always flip to `unavailable` in Home Assistant — HA keeps showing the **last numeric value it heard**. If Plenum averaged that stale value into a room temperature, it would confidently make the wrong control decision: wrong mode, vents closing on a room that hasn't actually reached target, no cycle starting on a room that's actually warm. The kind of silent failure that only shows up as a comfort complaint.

The engine treats a sensor reading as stale once its Home Assistant `last_updated` timestamp is older than the configured **sensor-staleness threshold** (default **30 minutes**, adjustable on the **Settings** page from 1 minute to 24 hours). Stale readings are excluded from `_get_avg_temp`, so they never contribute to the value that drives mode inference, vent-close decisions, or at-target checks. If every sensor in a room is stale, the room temperature falls through to the thermostat's own `current_temperature` attribute, exactly the same fallback used when a room has no sensors configured.

The engine refreshes the threshold at the start of each tick, so changes from the Settings page take effect within 60 seconds without a restart.

### UI surfacing

- **Dashboard** — a top-of-page banner appears the moment any configured room sensor crosses the threshold, listing each affected room and entity with its age. The banner disappears as soon as every sensor is fresh again.
- **Rooms page** — each room card carries a small orange "stale sensor" badge with a hover-tooltip naming each stale entity and how long it's been silent.
- **Settings page** — a "Sensor-staleness threshold" field lets you tune the value to your sensor population.
- **Event log** — the first tick a sensor crosses into staleness, the engine writes a `warning` event naming the entity and its age; further warnings for that same sensor are suppressed until it reports again, at which point a recovery `info` event is logged.

> The outdoor sensor used by the cooling lockout is read separately and is **not** gated by this staleness check — it serves analytics that tolerate slow updates (a weather entity may report hourly). The cooling lockout's existing fail-open behavior already covers a missing reading.

## Related thermostat safety limits

A few longer-standing limits on the [Thermostat settings](./thermostat-settings.md) page also protect equipment:

- **Min / max setpoint** — hard clamps; Plenum never commands the thermostat outside this range.
- **Min open vents** — always keep at least this many vents open, so the HVAC is never dead-headed.
- **Max vent closed** — force-reopen a vent after this long, a safety valve for systems that need airflow.
- **Cycle timeout** — abort a cycle that runs too long (stuck equipment, unreachable sensors).
