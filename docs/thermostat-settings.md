# Thermostat settings

Per-thermostat configuration controls how aggressively Plenum drives the HVAC and what safety limits apply. These live on the **Thermostats** page.

## Settings reference

| Setting | Default | What it does |
|---|---|---|
| **Default presence temp** | — | Fallback target °F for presence-activated rooms that have no room-level presence temp of their own. |
| **Min / max setpoint** | 60 / 85 °F | Hard clamps — Plenum will never ask HA to set the thermostat outside this range, even with overshoot applied. |
| **Deadband** | 0.5 °F | Tolerance for "room at target". `0.5` means a 70 °F target is satisfied at 69.5–70.5 °F. Set to `0` for exact match. A room and a schedule block can each override it — see [Deadband inheritance](#deadband-inheritance). |
| **Overshoot delta** | 2 °F | How far past the most demanding room's target Plenum sets the thermostat, to keep the HVAC running while easier rooms catch up. |
| **Cycle timeout** | 3 hours | A cycle running longer than this is aborted and vents are restored. Safety net for stuck equipment. |
| **Reconciliation interval** | 0 (disabled) | How often (minutes) Plenum re-reads actual vent and thermostat state from HA and corrects external overrides. `0` disables. Cannot exceed cycle timeout. |
| **Max vent closed** | 0 (disabled) | Force-reopen a vent after this many minutes even if its room is still at target. Safety valve for systems that need airflow for equipment protection. `0` disables. |
| **Total vent count** | — (required at registration) | Total registers on this thermostat — **smart vents AND passive ones**. Drives the airflow-floor calculation; see [Safety: Airflow floor](./safety.md#airflow-floor--dead-head-protection). |
| **I have a bypass damper** | unchecked | When ticked, the airflow floor is not enforced — the bypass damper mechanically relieves duct static pressure. |
| **Minimum open fraction** | 0.333 (one third) | Share of total registers that must stay open. Slider 0.1 – 1.0. Disabled when the bypass-damper box is ticked. |

## How these interact with a cycle

- **Overshoot** is applied when the cycle starts; the setpoint is restored to ambient when the cycle ends.
- **Deadband** is checked per-room at every tick to decide whether a room is calling for heating or cooling. It gates cycle start and mid-cycle join (including reopening a served room that has drifted a full band back past its target); a room's vents close at its exact target, not at the edge of the band.
- **Total vent count / minimum open fraction** together set the airflow floor — `ceil(total × fraction) − passive_vents_always_open` smart vents must stay open. If too many would close, the ones furthest from target stay open. Bypass-damper systems skip this enforcement.
- **Max vent closed** and **cycle timeout** are independent safety valves that can fire mid-cycle.

## Deadband inheritance

The **Deadband** above is the base of a three-level chain. A room can override it, and a schedule block can override the room. Most specific first:

1. The active schedule block's **Temperature drift** — only while that block is what has the room active. See [Schedules](./schedules.md#deadband-override).
2. The room's **Deadband override** — for that room, whatever activated it. See [Rooms & zones](./rooms-and-zones.md).
3. This **Deadband** — everything else.

Each override is optional and unset means "inherit the next one down", so a system that sets neither behaves exactly as it did before those fields existed. Both overrides are bounded to 0–10 °F. This setting is still what applies to any room with no room-level override, including rooms that are not active at all (overflow conditioning, the vent safety sweep).

## Interaction with pre-cool / pre-heat

The per-room [pre-cool / pre-heat](./precool-presence.md) feature builds on these: while a room coasts it uses a **widened deadband** (which must be ≥ this thermostat **Deadband**) on the side it is drifting from, and the **Min / max setpoint** act as the hard floor/ceiling that always overrides coasting.
