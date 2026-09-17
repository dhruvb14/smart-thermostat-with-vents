# Vacation mode

Vacation mode parks the whole house for a trip. Every room's schedules, presence triggers and temporary holds are **paused**, and each thermostat is held loosely inside its configured `min_setpoint`–`max_setpoint` envelope until the return date, at which point normal scheduling resumes automatically.

It is enabled from the banner or the **Manage** button on any page, and always requires a **return date and time** — there is no open-ended vacation. The mode applies to *all* thermostats at once; only the per-thermostat hold strategy below is configurable individually.

## Two hold strategies

Set per thermostat on the **Thermostats** page as **Vacation HVAC mode**.

### Single setpoint (default)

For thermostats that expose one target temperature at a time. Rather than turning the HVAC off, the hold **parks it in a recovered direction**: the same mode (heat or cool) as the thermostat's last completed cycle, or — before any cycle has run yet — whichever bound current ambient sits nearer to. The parked setpoint sits `overshoot_delta` degrees on the idle side of live ambient (above ambient when parked in cool, below it when parked in heat), so the equipment's own native hysteresis cannot call for heat or cooling on its own; only the engine decides when the trip resumes real conditioning. A bound breach still re-engages it fully — below `min_setpoint` it switches to heat, above `max_setpoint` it switches to cool — and once back inside the band it returns to parking rather than turning off.

**It triggers on the bound but recovers past it.** Crossing `min_setpoint` starts heating toward `min_setpoint + deadband`; crossing `max_setpoint` starts cooling toward `max_setpoint - deadband`. The margin is the point: recovering only to the bound itself means the hold arrives, is immediately no longer breaching, shuts the HVAC off, drifts back across and starts again — edge short-cycling, on a hold that runs unattended for days. This matches the [safety protection](./safety.md) margin used outside vacation. If `deadband` is wider than half the band, each target is clamped to the opposite bound so heating can never overshoot into calling for cooling.

Stopping the compressor also re-arms the [off-time lockout](./safety.md#off-time-lockout), so a second breach later in the same trip waits out `min_cycle_offtime_min` exactly as the first one would. The hold never creates a cycle, so before this it only inherited whatever lockout the trip started with.

This is the only strategy that can also watch each room's own sensor — see [Per-room safety cycles](#per-room-safety-cycles) below.

### Range (heat_cool / auto)

For thermostats that support `heat_cool` or `auto`. The thermostat is put into `heat_cool` with a lower bound of `min_setpoint` and an upper bound of `max_setpoint`, and manages both directions natively.

> **Range mode senses only the thermostat's own built-in ambient probe.** Your rooms' temperature sensors are not consulted, so a single room can drift well past its limits without the system reacting. If you want per-room protection while you are away, use **Single setpoint**.
>
> This is a structural limit, not an oversight. In `heat_cool` the *equipment* decides whether to heat or cool, from its own internal sensor. Plenum's cycle engine locks a cycle's direction at the moment the cycle starts and never re-derives it from the thermostat's live state — deriving direction from live `hvac_action` is exactly the bug that caused hours-long runaway cycles before it was fixed. Plenum cannot direct a single-room cycle through a thermostat whose direction it does not own, so it does not pretend to.

Range mode commands the bounds **as configured**, with no deadband inset — in `heat_cool` the equipment applies its own hysteresis, so there is no arrive-and-shut-off edge to cushion and insetting would only condition the house more tightly than you asked.

> **Range mode has no compressor off-time protection.** It cannot defer a start for the lockout, and it never re-arms one, because the hold never commands this thermostat off — there is no compressor-stop moment to measure from. Closing that gap would mean taking the heat/cool decision back off the equipment, which is the inversion bug described above. If your equipment needs short-cycle protection while you are away, use **Single setpoint**.

The **Test auto mode** button next to the selector puts the thermostat into `heat_cool` immediately so you can confirm it accepts the command, with a **Revert test** button to undo it.

## What the hold writes to the log

The hold re-evaluates every 60 seconds for as long as the trip lasts, so it logs **state changes, not ticks**. Each of these writes one line to the **Live Feed** on the Logs page, carrying the ambient reading and the bound that produced it:

| What happened | Level |
|---|---|
| Ambient fell below `min_setpoint` — holding heat at `min_setpoint + deadband` | info |
| Ambient rose above `max_setpoint` — holding cooling at `max_setpoint - deadband` | info |
| Ambient back inside the band — holding parked in `<mode>` at `<setpoint>` | info |
| Range mode took the thermostat, or its bounds changed | info |
| Cooling deferred by the compressor [off-time lockout](./safety.md#off-time-lockout) | warning |
| The thermostat is unavailable, so the hold issues no commands | warning |
| The thermostat is reachable but reports no ambient temperature | warning |
| Home Assistant rejected a hold command | error |

Repeated ticks in the same state add nothing, so a week away produces a handful of lines rather than ten thousand. Editing `min_setpoint` or `max_setpoint` mid-trip counts as a change and is logged with the new bound. Ending vacation mode closes the episode: a later trip logs its opening state again even if it matches the one the last trip ended in.

Per-room safety cycles below log separately, through normal cycle history.

## Per-room safety cycles

**Applies to `single` mode only. Default: on.** Toggle: **Run per-room safety cycles during vacation** on the Thermostats page.

The single-setpoint hold above watches the *thermostat's* ambient reading. That leaves a gap: one room can bake or freeze well past its limits while the thermostat's own probe — often in a hallway — sits comfortably inside the band and reports nothing wrong.

With per-room safety cycles on, any room whose **own sensor** leaves the envelope starts a real cycle through the normal engine, exactly as the [#367 safety protection](./safety.md) does outside vacation:

- it appears in **Cycle History** and the **event log**, with `source: safety`;
- the recovery target is one deadband inside the breached bound (`max_setpoint − deadband` when cooling, `min_setpoint + deadband` when heating), so the room returns with a hysteresis margin instead of hovering on the limit;
- short-cycle protection, the cycle timeout, the airflow floor and drift correction all apply as usual.

**Schedules, presence and temporary holds stay paused.** A safety breach is the only thing that may create demand during vacation — the mode is a demand *filter*, not a switch back to normal operation with wider bounds.

### How the air is shared

With nobody home there is no comfort target to protect, so a vacation safety cycle runs the zone **wide open**: every other room's vent stays open and shares the conditioned air, rather than being closed at cycle start the way a normal cycle closes its idle rooms.

Each shared room keeps absorbing until it comes within its own deadband of the **opposite** bound, at which point its vent closes so it cannot overshoot into calling for the opposite cycle:

| Cycle direction | A shared room's vent closes when |
|---|---|
| Cooling | `room_temp ≤ min_setpoint + deadband` |
| Heating | `room_temp ≥ max_setpoint − deadband` |

`deadband` is the room's own `deadband_override` when set, falling back to the thermostat's, matching the [deadband inheritance](./thermostat-settings.md#deadband-inheritance) used everywhere else.

Worked example — ceiling 78 °F, floor 68 °F, deadband 2 °F:

1. The gym reaches 85 °F, 7 °F over the ceiling. A cooling cycle starts, targeting 76 °F.
2. A bedroom at 74 °F is nowhere near the 68 °F floor, so its vent stays open and it shares the cooling.
3. The bedroom reaches 70 °F — the floor plus its deadband. Its vent closes; the gym keeps cooling toward 76 °F.

Two rules outrank this policy:

- **The [airflow floor](./safety.md#airflow-floor--dead-head-protection) always wins.** If closing a satisfied room's vent would drop the zone below its minimum open-vent count, the vent stays open. Closes are ordered most-satisfied-first, so the vents that remain open belong to the rooms with the most headroom left.
- **A room with no readable sensor keeps its vent open.** An unreadable room cannot be shown to be near its bound, and restricting airflow is the more dangerous error.

This is deliberately *not* the tiered [overflow conditioning](./overflow-conditioning.md) used during the minimum-runtime hold. Those tiers rank rooms against their *comfort* setpoint and skip any room that has none configured — during vacation that is both the wrong question and the wrong exclusion. Overflow conditioning remains disabled in vacation mode.

### Turning it off

Untick the toggle to fall back to holding the whole house on the thermostat's own sensor alone — the behaviour from before this feature existed. The setting is per thermostat, so you can leave it on for a zone with good room sensors and off for one without.

## Ending vacation mode

Vacation mode ends automatically at the return date, and normal scheduling resumes on the next tick. To end it early, open the banner and choose **End vacation mode early**.

## Related

- [Safety features](./safety.md) — the envelope, the airflow floor, short-cycle protection
- [Thermostat settings](./thermostat-settings.md) — `min_setpoint`, `max_setpoint`, deadband inheritance
- [Overflow conditioning](./overflow-conditioning.md) — the tiered policy used outside vacation
- [MQTT interface](./mqtt.md) — flipping vacation mode from a Home Assistant automation
