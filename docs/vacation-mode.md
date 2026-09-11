# Vacation mode

Vacation mode parks the whole house for a trip. Every room's schedules, presence triggers and temporary holds are **paused**, and each thermostat is held loosely inside its configured `min_setpoint`–`max_setpoint` envelope until the return date, at which point normal scheduling resumes automatically.

It is enabled from the banner or the **Manage** button on any page, and always requires a **return date and time** — there is no open-ended vacation. The mode applies to *all* thermostats at once; only the per-thermostat hold strategy below is configurable individually.

## Two hold strategies

Set per thermostat on the **Thermostats** page as **Vacation HVAC mode**.

### Single setpoint (default)

For thermostats that expose one target temperature at a time. The HVAC is turned **off** and re-engaged only when a bound is breached: below `min_setpoint` it switches to heat, above `max_setpoint` it switches to cool, and once back inside the band it turns off again.

This is the only strategy that can also watch each room's own sensor — see [Per-room safety cycles](#per-room-safety-cycles) below.

### Range (heat_cool / auto)

For thermostats that support `heat_cool` or `auto`. The thermostat is put into `heat_cool` with a lower bound of `min_setpoint` and an upper bound of `max_setpoint`, and manages both directions natively.

> **Range mode senses only the thermostat's own built-in ambient probe.** Your rooms' temperature sensors are not consulted, so a single room can drift well past its limits without the system reacting. If you want per-room protection while you are away, use **Single setpoint**.
>
> This is a structural limit, not an oversight. In `heat_cool` the *equipment* decides whether to heat or cool, from its own internal sensor. Plenum's cycle engine locks a cycle's direction at the moment the cycle starts and never re-derives it from the thermostat's live state — deriving direction from live `hvac_action` is exactly the bug that caused hours-long runaway cycles before it was fixed. Plenum cannot direct a single-room cycle through a thermostat whose direction it does not own, so it does not pretend to.

The **Test auto mode** button next to the selector puts the thermostat into `heat_cool` immediately so you can confirm it accepts the command, with a **Revert test** button to undo it.

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
