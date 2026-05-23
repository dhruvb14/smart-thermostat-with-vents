# Thermostat settings

Per-thermostat configuration controls how aggressively Plenum drives the HVAC and what safety limits apply. These live on the **Thermostats** page.

## Settings reference

| Setting | Default | What it does |
|---|---|---|
| **Default presence temp** | — | Fallback target °F for presence-activated rooms that have no room-level presence temp of their own. |
| **Min / max setpoint** | 60 / 85 °F | Hard clamps — Plenum will never ask HA to set the thermostat outside this range, even with overshoot applied. |
| **Deadband** | 0.5 °F | Tolerance for "room at target". `0.5` means a 70 °F target is satisfied at 69.5–70.5 °F. Set to `0` for exact match. |
| **Overshoot delta** | 2 °F | How far past the most demanding room's target Plenum sets the thermostat, to keep the HVAC running while easier rooms catch up. |
| **Cycle timeout** | 3 hours | A cycle running longer than this is aborted and vents are restored. Safety net for stuck equipment. |
| **Reconciliation interval** | 0 (disabled) | How often (minutes) Plenum re-reads actual vent and thermostat state from HA and corrects external overrides. `0` disables. Cannot exceed cycle timeout. |
| **Max vent closed** | 0 (disabled) | Force-reopen a vent after this many minutes even if its room is still at target. Safety valve for systems that need airflow for equipment protection. `0` disables. |
| **Total vent count** | — (required at registration) | Total registers on this thermostat — **smart vents AND passive ones**. Drives the airflow-floor calculation; see [Safety: Airflow floor](./safety.md#airflow-floor--dead-head-protection). |
| **I have a bypass damper** | unchecked | When ticked, the airflow floor is not enforced — the bypass damper mechanically relieves duct static pressure. |
| **Minimum open fraction** | 0.333 (one third) | Share of total registers that must stay open. Slider 0.1 – 1.0. Disabled when the bypass-damper box is ticked. |

## How these interact with a cycle

- **Overshoot** is applied when the cycle starts; the setpoint is restored to ambient when the cycle ends.
- **Deadband** is checked per-room at every tick to decide when to close a room's vents.
- **Total vent count / minimum open fraction** together set the airflow floor — `ceil(total × fraction) − passive_vents_always_open` smart vents must stay open. If too many would close, the ones furthest from target stay open. Bypass-damper systems skip this enforcement.
- **Max vent closed** and **cycle timeout** are independent safety valves that can fire mid-cycle.
