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

## In-place cycle updates

A room's target or source can change while a cycle is already running — you edit a scheduled block's temperature, or a presence holdover gives way to a scheduled block for the same room. The engine used to handle this by **tearing the whole cycle down** and rebuilding it on the next tick.

That teardown is itself an unnecessary compressor stop/start — the very short-cycling the protection above exists to prevent. Worse, with the off-time lockout enabled, the rebuild was then blocked for the lockout window, leaving the room with no conditioning it still needed.

The engine now applies such a change **in place**: it updates the running cycle's target and re-derives the thermostat setpoint without stopping the HVAC. The cycle log stays open and an `updated in place` entry is written to the event log. There is nothing to configure — applying the change in place is simply correct. A genuine *direction flip* (a room that now needs the opposite of the locked cycle mode) is still handled separately: the mode filter drops that room from the cycle. See the [cycle engine guide](./cycle-engine.md#mid-cycle-trigger-changes) for the tick-by-tick detail.

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

## Airflow floor / dead-head protection

Closing too many vents at once raises **duct static pressure**. Past a certain point that trips the furnace high-limit, strains the air handler's blower, or — on a cooling cycle — drops evaporator-coil temperature enough to freeze it. The airflow floor keeps a fraction of the home's total registers open at all times so the system can never dead-head itself.

### Three fields drive it

Configured per thermostat on the **Thermostats** page:

| Field | Default | Role |
|---|---|---|
| **Total vent count** | — (required at registration) | Total registers on the thermostat — **smart vents AND passive ones, not only smart vents**. Passive registers are always open and reduce how many of the smart vents have to stay open. |
| **I have a bypass damper** | unchecked | A bypass damper is a mechanical relief valve that opens when duct static pressure exceeds a setpoint. When ticked, the airflow floor is not enforced — the damper handles pressure relief. Most residential systems do *not* have one. |
| **Minimum open fraction** | **1/3 (0.333)** | Share of total registers that must stay open. Slider 0.1 – 1.0 with the live percentage shown next to it. Disabled when the bypass-damper box is ticked. |

### How the engine uses it

Each tick the engine computes:

```
required_smart_open = max(
    0,
    ceil(total_vents_count × fraction) − (total_vents_count − smart_vents_count)
)
```

`smart_vents_count` is the live count of smart vents configured across the thermostat's rooms. The right-hand subtraction is the number of passive registers — always-open — which already contribute to the airflow floor. The clamp at zero handles cases where there are enough passive vents to satisfy the floor on their own.

A worked example. A floor has 12 total registers and 4 of them are smart, with the default 1/3 fraction:

- `ceil(12 × 1/3) = 4` registers must stay open in total.
- `12 − 4 = 8` passive registers are always open.
- `4 − 8 = −4`, clamped to **0** — all four smart vents may close. The 8 passive registers already satisfy the airflow floor by themselves.

On a 4/4 system (every register is smart) with the same fraction: `ceil(4 × 1/3) − 0 = 2` smart vents must stay open. The engine defers closing a vent that would drop below this floor, except for the [last-vent bypass](./cycle-engine.md) which closes the final vent anyway to let the cycle terminate — the brief dead-head window is paired with an immediate setpoint-to-ambient reset so the HVAC is no longer commanded on.

### Bypass damper

If you have one, tick the box. The slider greys out with the note *"Not enforced — your bypass damper handles pressure relief."* The engine returns 0 for `required_smart_open`, allowing any number of smart vents to close.

### Upgrade banner

Thermostats registered before this safety shipped have `total_vents_count = null`. The engine treats them as the **transitional default** of 1 (the pre-#213 `min_open_vents` default) so they keep working through the upgrade window. A warning alert at the top of the **Dashboard** and **Thermostats** pages lists those thermostats and asks the user to either fill in the total or tick the bypass-damper box. The banner disappears as soon as both fields are valid.

## Related thermostat safety limits

A few longer-standing limits on the [Thermostat settings](./thermostat-settings.md) page also protect equipment:

- **Min / max setpoint** — hard clamps; Plenum never commands the thermostat outside this range.
- **Min open vents** — always keep at least this many vents open, so the HVAC is never dead-headed.
- **Max vent closed** — force-reopen a vent after this long, a safety valve for systems that need airflow.
- **Cycle timeout** — abort a cycle that runs too long (stuck equipment, unreachable sensors).
