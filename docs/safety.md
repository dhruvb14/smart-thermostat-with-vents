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

## Related thermostat safety limits

A few longer-standing limits on the [Thermostat settings](./thermostat-settings.md) page also protect equipment:

- **Min / max setpoint** — hard clamps; Plenum never commands the thermostat outside this range.
- **Min open vents** — always keep at least this many vents open, so the HVAC is never dead-headed.
- **Max vent closed** — force-reopen a vent after this long, a safety valve for systems that need airflow.
- **Cycle timeout** — abort a cycle that runs too long (stuck equipment, unreachable sensors).
