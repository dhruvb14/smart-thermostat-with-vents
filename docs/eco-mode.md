# Eco Mode — outdoor-temperature-compensated setpoint drift

Eco Mode relaxes a room's requested temperature target based on how extreme it
is outside, so the HVAC works less exactly when it is fighting the biggest
outdoor load. It is configured **globally per thermostat** and **overridable per
room**, and is symmetric for cooling and heating. It is **off by default** — when
off, the engine behaves exactly as it did before this feature existed.

> **Why it saves energy.** On a 95 °F afternoon a bedroom asking for 70 °F often
> can't reach 70 °F anyway — the compressor runs flat-out and never satisfies.
> Letting the target float up to 74 °F while it's that hot means the room is
> "at target" sooner, so the cycle ends instead of dead-heading. Each +1 °F of
> cooling setpoint is roughly 3–5 % less cooling energy. Heating is the mirror
> image on cold days. Background analysis: issues #402 and #404.

## Requires an outside-temperature sensor

Eco Mode is entirely driven by the outdoor temperature, so **it cannot be turned
on until the house-wide outside-temperature entity is configured** (set it at the
top of the Thermostats page). The UI disables the Eco toggle until then, and the
API rejects enabling it without a sensor.

If you do not have a physical outdoor thermometer in Home Assistant, add a free
weather integration — **[PirateWeather](https://pirateweather.net/)** is a good
drop-in (Met.no and OpenWeatherMap work too) — and point the
outside-temperature setting at its temperature sensor.

If the sensor later becomes unreadable (unavailable/stale), Eco Mode simply does
nothing that cycle — the room runs to its real requested target.

## The drift model — a proportional ramp

Relaxation scales with how far past the threshold it is outside, reaching the
configured **max drift** at a configurable **full-drift** outdoor temperature.

**Cooling** (relax the target *up* / warmer):

```
f      = clamp((T_out − cool_threshold) / (cool_full_drift − cool_threshold), 0, 1)
drift  = f × cool_max_drift
target = requested_target + drift          (clamped to min/max setpoint)
```

**Heating** (relax the target *down* / cooler):

```
f      = clamp((heat_threshold − T_out) / (heat_threshold − heat_full_drift), 0, 1)
drift  = f × heat_max_drift
target = requested_target − drift          (clamped to min/max setpoint)
```

Cooling relaxes warmer and heating relaxes cooler, so the two targets always
move **apart** — Eco Mode can never create an opposite-direction heat↔cool
conflict.

**Hard step.** Set the full-drift temperature **equal to** the threshold to get a
hard step instead of a ramp: the target jumps straight to the full drift the
moment the threshold is crossed.

### Worked example — cooling (°F)

Threshold 86 °F, full-drift 100 °F, max drift 4 °F, a bedroom asking for 70 °F:

| Outside | f | Effective target |
|---|---|---|
| ≤ 86 °F | 0 | **70 °F** (unchanged) |
| 93 °F | 0.5 | **72 °F** |
| ≥ 100 °F | 1 | **74 °F** |

### Worked example — cooling (°C)

The same defaults read round in Celsius: threshold 30 °C, full-drift 38 °C, max
drift 2 °C, a bedroom asking for 21 °C:

| Outside | f | Effective target |
|---|---|---|
| ≤ 30 °C | 0 | **21 °C** (unchanged) |
| 34 °C | 0.5 | **22 °C** |
| ≥ 38 °C | 1 | **23 °C** |

### Worked example — heating (°F / °C)

Threshold 40 °F / 4 °C, full-drift 0 °F / −18 °C, max drift 4 °F / 2 °C, a room
asking for 70 °F / 21 °C:

| Outside (°F) | Effective (°F) | | Outside (°C) | Effective (°C) |
|---|---|---|---|---|
| ≥ 40 | 70 (unchanged) | | ≥ 4 | 21 (unchanged) |
| 20 | 68 | | −7 | 20 |
| ≤ 0 | 66 | | ≤ −18 | 19 |

### Hysteresis

A small **hysteresis band** stops the relaxation from flapping right at the
threshold: once relaxing begins at the threshold, it keeps relaxing until outside
falls to `threshold − band` (cooling) or rises to `threshold + band` (heating).
This matters most for the hard-step configuration.

The effective target is computed **at the start of a cycle / at a cycle
boundary**, not on every tick, so a cycle's target does not churn mid-run.

## Configuration

### Fields

Eight fields live on every thermostat (non-null, defaulted) and are mirrored on
each room as **nullable** overrides.

| Field | Kind | Meaning |
|---|---|---|
| `eco_mode_enabled` | bool | Master toggle |
| `eco_cooling_outdoor_threshold` | °F absolute (outdoor) | Start relaxing cooling above this |
| `eco_cooling_full_drift_temp` | °F absolute (outdoor) | Full drift reached at this |
| `eco_cooling_max_drift` | °F delta | Max upward cooling drift |
| `eco_heating_outdoor_threshold` | °F absolute (outdoor) | Start relaxing heating below this |
| `eco_heating_full_drift_temp` | °F absolute (outdoor) | Full drift reached at this |
| `eco_heating_max_drift` | °F delta | Max downward heating drift |
| `eco_hysteresis_band` | °F delta | Anti-flapping band around the threshold |

### Per-room overrides — field-level null-inheritance

On a room, every field defaults to **inherit** (blank in the UI, `null` in the
API). Set a value to override **just that one field**; all other fields still
inherit the thermostat. `eco_mode_enabled` is a tri-state:

- **Inherit** — follow the thermostat toggle.
- **On** — run Eco for this room even if the thermostat has it off.
- **Off** — opt this room out even if the thermostat has it on.

So a single always-calling room can be relaxed independently of the rest of the
zone.

### Defaults — round in both units

Storage is always °F (see [conventions](./README.md)). Because one stored °F
value can't read round in both units, default seeding is unit-aware: a home set
up in °C seeds the clean Celsius numbers, a °F home seeds the Fahrenheit ones. A
later unit switch never rewrites stored values.

| Field | °F default | °C default |
|---|---|---|
| Cooling threshold | 86 | 30 |
| Cooling full-drift | 100 | 38 |
| Cooling max drift | 4 | 2 |
| Heating threshold | 40 | 4 |
| Heating full-drift | 0 | −18 |
| Heating max drift | 4 | 2 |
| Hysteresis band | 2 | 1 |

## Safety invariants

- **Off = no change.** With Eco off (the default) the engine takes the exact
  pre-Eco path: the setpoint sequence, overshoot, vent logic, and cycle
  lifecycle/timeouts are all identical.
- Relaxation is **clamped to the thermostat's `min_setpoint` / `max_setpoint`**.
- Cooling and heating targets move apart, so Eco can never trigger an
  opposite-direction cycle.
- Overshoot (`overshoot_delta`) still applies on top of the relaxed target.
- No/stale/unreadable outdoor reading → Eco is a no-op for that cycle.

## Where you see it

- **Dashboard** — a relaxed room shows both the requested and the effective
  target, attributed to Eco Mode (e.g. `🌿 72°F · requested 70°F — Eco`).
- **Logs** — each affected cycle carries an **Eco Mode** pill, and the per-room
  detail shows `target → 🌿 effective` so you can see exactly what was relaxed.

## Measuring the impact

Every room-cycle records the `requested_target`, the `effective_target`, and
whether Eco was active. Query the impact over any date range:

- `GET /api/metrics/thermostats/{id}/eco-impact` (and the home-wide
  `GET /api/metrics/thermostats/eco-impact`) — cycles and runtime split by
  whether Eco relaxed a target, the average drift applied, and a per-room
  breakdown.
- The cycles-vs-outside-temp scatter and the thermostat summary also carry Eco
  fields, so you can trend drift against outdoor temperature.

All of these are exposed as [MCP](./mcp.md) tools, so you can mine the collected
data for savings after rollout. Because Plenum can't read kWh, savings are
inferred from **runtime reduction**, not measured directly.

## See also

- [Thermostat settings](./thermostat-settings.md) — the outside-temperature sensor and safety envelope
- [Pre-cool / pre-heat](./precool-presence.md) — a related outdoor-aware, per-room energy feature
- [Cycle engine](./cycle-engine.md) — how a cycle runs and where the target becomes a setpoint
- [Metrics](./metrics.md) — the metrics API and MCP tools
