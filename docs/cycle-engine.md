# Cycle engine

One `CycleEngine` instance runs per thermostat. It ticks every 60 seconds and drives exactly one HVAC cycle at a time through an `IDLE → RUNNING → TERMINATING → IDLE` state machine.

## What a tick does

1. **Resolve active rooms.** For each room in the zone, the room manager picks the active target by priority: manual override → schedule → presence holdover. Rooms with no active target are idle.
2. **Infer mode.** Compare each active room's average temperature (plus offset) to its target. If rooms need heating, the cycle runs in heat; if cooling, in cool. Rooms asking for the opposite direction are dropped from this cycle.
3. **Open vents for participating rooms**, close idle-room vents (subject to `min_open_vents`).
4. **Set the thermostat setpoint** past the most demanding room's target by the configured **overshoot** delta, so the HVAC keeps running after the easier rooms are satisfied.
5. **Monitor** each room's average temperature. When a room hits target (within the deadband), its vents close.
6. **End the cycle** once every room is at target — the thermostat setpoint is restored to its own ambient reading so the HVAC shuts off naturally.

## Why the mode is locked at cycle start

Once a cycle starts in heat or cool, the mode is locked. This prevents oscillation during HVAC idle phases (when no room is actively demanding, the "naive" inferred mode can flip frame-to-frame). A cycle ends cleanly and a new one can start in the opposite direction on the next tick.

## Mid-cycle trigger changes

A room's **trigger** — its `source` (override / schedule / presence) and its `target_temp` — can change while a cycle is running: a presence holdover gives way to a scheduled block, or you edit a schedule's target. The engine applies such a change **in place** — it updates the room's target/source on the running cycle and re-derives the thermostat setpoint, without stopping the HVAC. The cycle log stays open and its room snapshot is updated to reflect the new trigger. The update is visible in two places: the cycle's setpoint history (on the Logs page cycle-detail view) records a `trigger updated in place` entry, and an `updated in place` line is written to the [event log](./observability.md).

The cycle is **not** torn down for a trigger change. A teardown would stop the compressor, and with [short-cycle protection](./safety.md) enabled the off-time lockout could then refuse to restart it for several minutes — leaving a room with no conditioning it still needs. A genuine *direction flip* (a room that now needs the opposite of the locked cycle mode) is a separate case, caught earlier: the mode filter drops that room from the cycle, and if no compatible rooms remain the cycle ends.

## Reconciliation

If **reconciliation interval** is set on the thermostat, the engine re-reads the actual state of every vent and the thermostat setpoint from HA mid-cycle on that cadence, and corrects any external overrides (someone using another integration, or a manual tap on the thermostat). Set to `0` to disable.

## Cycle timeout

A cycle running longer than **cycle timeout** hours is aborted, vents are restored, and a warning is logged. This is a safety net for misconfigured overshoot, stuck HVAC equipment, or unreachable sensors.

## What gets persisted

Every cycle writes a row to the cycle log with start/end timestamps, mode, rooms involved, and per-room state snapshots (reached-target time, vent-closed time, joined-mid-cycle time). Temperature samples and setpoint changes are also captured for the cycle-history drilldown. See [Observability](./observability.md).
