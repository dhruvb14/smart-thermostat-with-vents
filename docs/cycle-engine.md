# Cycle engine

One `CycleEngine` instance runs per thermostat. It ticks every 60 seconds and drives exactly one HVAC cycle at a time through an `IDLE → RUNNING → TERMINATING → IDLE` state machine.

## What a tick does

1. **Resolve active rooms.** For each room in the zone, the room manager picks the active target by priority: manual override → schedule → presence holdover. Rooms with no active target are idle.
2. **Infer mode.** Compare each active room's average temperature (plus offset) to its target. If rooms need heating, the cycle runs in heat; if cooling, in cool. Rooms asking for the opposite direction are dropped from this cycle.
3. **Open vents for participating rooms**, close idle-room vents (subject to `min_open_vents_fraction`).
4. **Set the thermostat setpoint** past the most demanding room's target by the configured **overshoot** delta, so the HVAC keeps running after the easier rooms are satisfied.
5. **Monitor** each room's average temperature. When a room hits target (within the deadband), its vents close. A served room re-crossing its target by a small amount is hysteresis noise and does not reopen it (#86) — but if it drifts a full deadband past target, it has live demand again and the engine reopens its vent to resume serving it in this cycle (#503).
6. **End the cycle** once every room is at target — the thermostat setpoint is parked at its own ambient reading nudged by the **overshoot** delta to the idle side of the cycle direction (cooling parks above ambient, heating below), so the HVAC shuts off and cannot self-restart on its own hysteresis before the engine sees real room demand and starts the next cycle.

## Why the mode is locked at cycle start

Once a cycle starts in heat or cool, the mode is locked. This prevents oscillation during HVAC idle phases (when no room is actively demanding, the "naive" inferred mode can flip frame-to-frame). A cycle ends cleanly and a new one can start in the opposite direction on the next tick.

## Mid-cycle trigger changes

A room's **trigger** — its `source` (override / schedule / presence) and its `target_temp` — can change while a cycle is running: a presence holdover gives way to a scheduled block, or you edit a schedule's target. The engine applies such a change **in place** — it updates the room's target/source on the running cycle and re-derives the thermostat setpoint, without stopping the HVAC. The cycle log stays open and its room snapshot is updated to reflect the new trigger. The update is visible in two places: the cycle's setpoint history (on the Logs page cycle-detail view) records a `trigger updated in place` entry, and an `updated in place` line is written to the [event log](./observability.md).

The cycle is **not** torn down for a trigger change. A teardown would stop the compressor, and with [short-cycle protection](./safety.md) enabled the off-time lockout could then refuse to restart it for several minutes — leaving a room with no conditioning it still needs. A genuine *direction flip* (a room that now needs the opposite of the locked cycle mode) is a separate case, caught earlier: the mode filter drops that room from the cycle, and if no compatible rooms remain the cycle ends.

## Reconciliation

If **reconciliation interval** is set on the thermostat, the engine re-reads the actual state of every vent and the thermostat setpoint from HA mid-cycle on that cadence, and corrects any external overrides (someone using another integration, or a manual tap on the thermostat). Set to `0` to disable.

## Cycle timeout

A cycle running longer than **cycle timeout** hours is aborted, vents are restored, and a warning is logged. This is a safety net for misconfigured overshoot, stuck HVAC equipment, or unreachable sensors.

## Minimum-runtime hold

A cycle that satisfies every active room in less time than the configured **minimum cycle runtime** is held open through the rest of the runtime window rather than stopped early — short-cycling a compressor is a primary equipment-failure mode. See [Safety features](./safety.md#short-cycle-protection) for the safety reasoning.

During the hold:

- The cycle is flagged `in_min_runtime_hold` on its cycle-log row. Every monitoring tick checks the flag and **short-circuits the per-room close-vent loop** so vents the hold just opened cannot be re-closed on the next tick (which used to produce open/close churn through the hold window).
- Vents for the originally-active rooms stay open so the air handler has a full duct path — no dead-heading through whichever room finished last.
- If [overflow conditioning](./overflow-conditioning.md) is enabled, non-active rooms that can absorb the surplus air (without crossing into the opposite-direction trigger) also have their vents opened for the remainder of the hold.
- Once the runtime clock is satisfied the cycle terminates normally, all zone vents return to the open idle state, and the off-time lockout begins.

The `in_min_runtime_hold` flag is persisted, so a server restart mid-hold resumes the hold rather than starting fresh.

## What gets persisted

Every cycle writes a row to the cycle log with start/end timestamps, mode, rooms involved, and per-room state snapshots (reached-target time, vent-closed time, joined-mid-cycle time). Temperature samples and setpoint changes are also captured for the cycle-history drilldown. See [Observability](./observability.md).
