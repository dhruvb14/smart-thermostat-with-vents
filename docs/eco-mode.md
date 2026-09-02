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

### Fractional targets vs. whole-degree setpoint commands

Mid-ramp the relaxed target is usually fractional (70 °F requested, ramp
fraction 0.65 × 4 °F drift → **72.6 °F**). That fractional value **is** the
room's effective target: the cycle runs until the room actually reaches
72.6 °F, and it is what the Logs and Dashboard display next to the 🌿 badge.

Most thermostats, however, do not support partial-degree setpoints — command
70.28 °F and the device stores 70 °F, which the reconciler then reads as
permanent external drift and re-asserts every pass. So every setpoint the
engine **commands to the device** is rounded to a whole degree at the command
boundary, but the direction depends on the role: the cycle setpoint (relaxed
target ± overshoot) and the mid-cycle ambient-anchored overshoot correction
round **toward the driving room** — floored for cooling, ceiled for heating
(`floor_whole_f` / `ceil_whole_f`) — so the command can never land on the idle
side of a fractional target and stop the HVAC before the room gets there.
Idle parking has no room target to protect, so it rounds to the closest whole
degree, halves up (`round_whole_f`). In the example above with a 2 °F
overshoot delta, the room runs to 72.6 °F while the thermostat is commanded
72.6 − 2 = 70.6 → floored → **70 °F**.

The rounding deliberately does NOT touch the relaxed target itself: rounding
the target silently rewrites the room's ask (72.6 °F becomes 73 °F) and makes
the zone run past the temperature Eco actually computed.

### Hysteresis

A small **hysteresis band** stops the relaxation from flapping right at the
threshold: once relaxing begins at the threshold, it keeps relaxing until outside
falls to `threshold − band` (cooling) or rises to `threshold + band` (heating).
In practice the band only changes the *target* for the hard-step
configuration: with a ramp, an engaged room inside the band sits at ramp
fraction 0 (zero drift), so the band's job there is just to keep the
engagement latched rather than flapping on/off at the threshold.

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

## What Eco never touches

- **Manual overrides — unless the hold opts in.** An override is the strongest
  user signal there is — "this room, this temperature, right now" — so by
  default Eco never relaxes it (#419; the same explicit-intent rule
  [pre-cool](./precool-presence.md) applies). A [temperature
  hold](./temperature-holds.md) can opt itself in via its **Allow Eco Mode to
  relax this hold** checkbox (`respect_eco`, #576) and is then relaxed exactly
  like a scheduled room — an active Eco Suspend or a per-room Eco **Off**
  still prevents relaxation. Schedules remain relaxable; to opt a scheduled
  room out, set its per-room Eco toggle to **Off**.
- **Safety-protection targets.** A room pulled into a cycle because it breached
  the min/max setpoint envelope is recovered to its protective target
  unmodified — relaxing a recovery bound on the hottest days would defeat the
  protection.

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
- **Metrics** — a dedicated **🌿 Eco Mode impact** section (home-wide and
  per-thermostat): tiles for eco-relaxed cycles, eco runtime share, average
  drift, and a rule-of-thumb savings estimate, plus eco-vs-standard cycles per
  day, average drift per day, and a per-room drift breakdown. The
  cycles-vs-outside-temp scatter colors Eco-relaxed cycles green.

## Temporarily suspending Eco (Eco Suspend, Issue #500)

Sometimes the house needs to actually hit its targets — a party with a full
house, guests staying over — and relaxed targets are exactly wrong. **Eco
Suspend** turns Eco Mode's relaxation off **per thermostat** until a date/time
you pick, then resumes automatically. Modeled on
[vacation mode](./system-modes.md)'s self-expiring hold:

- **Per thermostat, explicitly chosen.** Each thermostat has at most one
  active suspension with its own resume time. The shared modal carries a
  thermostat picker; the Dashboard zone-card and Thermostats-card controls
  open it pre-scoped.
- **Zone-wide.** While suspended, Eco is fully off for every room under that
  thermostat — including rooms whose per-room tri-state override explicitly
  opts them in. The use case is "hit real targets everywhere in this zone".
- **Next cycle only.** A cycle already running when a suspension starts (or
  ends/expires) finishes under the Eco state it started with; new cycles
  evaluate the suspension at cycle start. Gentler on the HVAC than an
  immediate re-evaluate.
- **Nothing is modified.** The Eco enable flag and all tuning fields —
  thermostat- and room-level — are untouched; a suspended cycle follows the
  exact eco-off code path (`eco_active` stays false, so the Metrics impact
  numbers stay honest). Hysteresis engagement memory also survives — a
  suspension is a pause, not a reset.
- **Self-expiring and restart-safe.** The suspension persists in its own DB
  table (never clobbered by a config save) and a scheduler sweep clears it
  when the resume time passes.

**Where:** the 🍃 buttons on the Dashboard (page level + per zone card) and the
Thermostats page (page level + per card header), and a green 🍃 banner shown on
every page while any thermostat is suspended — click it to edit or resume
early. The buttons only appear when Eco is actually in play: a thermostat has
Eco enabled, a room carries an explicit per-room Eco opt-in, or a suspension
is already active. With Eco off everywhere there is nothing to suspend, so
nothing is shown.

**API:** `POST /api/thermostats/{entity_id}/eco-suspend` with
`{"resume_at": "<ISO-8601>"}` (posting again replaces — that's the edit path)
and `DELETE /api/thermostats/{entity_id}/eco-suspend` to resume now. Active
suspensions surface on `GET /api/settings` (`eco_suspend` map) and as the
read-only `eco_suspend_until` on `GET /api/thermostats`.

## Measuring the impact

Every room-cycle records the `requested_target`, the `effective_target`, and
whether Eco was active. Query the impact over any date range:

- `GET /api/metrics/thermostats/{id}/eco-impact` (and the home-wide
  `GET /api/metrics/thermostats/eco-impact`) — cycles and runtime split by
  whether Eco relaxed a target, the average drift applied, a per-day `days`
  series of the same split (feeding the Metrics-page trend charts), and a
  per-room breakdown.
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
