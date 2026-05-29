# Overflow conditioning during minimum-runtime hold

When a cycle reaches its goal early (every active room is at target before the configured **minimum cycle runtime** has elapsed), the compressor must keep running until the runtime clock is satisfied — short-cycling is a primary equipment-failure mode. The leftover conditioning still has to go somewhere. Plenum routes it through up to three tiers of fallback candidates, opening additional vents in non-active rooms that can absorb the surplus without being driven into an opposite-direction cycle.

> **Scope:** This logic runs **only** during the minimum-runtime hold phase. Normal cycle operation is unchanged, and after the cycle fully terminates every zone vent returns to the open idle state regardless of which vents the hold opened.

## Why not just keep the active rooms' vents open?

Before this feature, the hold kept only the originally-active rooms' vents open. That works, but has two failure modes:

1. **Overshoot.** The active rooms continue to receive cold (or hot) air past their target, drifting well below (or above) the setpoint. If the drift is large enough, those same rooms call for the *opposite* direction on the next cycle — the kind of oscillation the cycle engine is otherwise designed to prevent.
2. **Restricted airflow.** With only one or two rooms' vents open and no bypass damper, duct static pressure rises and the air handler dead-heads through whichever rooms finished last.

Opening additional vents in rooms that can absorb the surplus addresses both: it reduces overshoot in the active rooms, gives the air handler a larger plenum, and pre-conditions rooms toward where they'd want to be when their schedule or presence next activates.

## The four tiers

For each non-active room on the same thermostat, Plenum computes an **effective presence setpoint** (see "Setpoint resolution" below) and tries up to three filtering tiers in order. The first tier that yields any candidates wins; every candidate in that tier has its vent opened. If all three are empty, the system falls back to today's behaviour.

Throughout, `deadband` refers to the same `ThermostatConfig.deadband` (`±°F`) that gates cycle start/stop, and `active_cycle_target_f` is the most aggressive target across the cycle's active rooms — the lowest for cooling, the highest for heating.

### Tier 1 — Surplus rooms (outside deadband)

A non-active room qualifies if:

- **Cooling:** `room.current_temp > effective_setpoint + deadband` **and** `room.current_temp > active_cycle_target_f`
- **Heating:** `room.current_temp < effective_setpoint − deadband` **and** `room.current_temp < active_cycle_target_f`

These are clear wins: the room is past its setpoint in the same direction the system is currently conditioning, by more than the deadband, and is on the right side of the active cycle target.

### Tier 2 — Inside-deadband rooms

Same direction check, but without the deadband margin. Used only when Tier 1 is empty (every non-active room is already within deadband of its setpoint). This lets Plenum nudge a room a little further past its setpoint when there's no better destination — the room is still moving in the right direction, just not by much.

### Tier 3 — Headroom rooms (accept pushing past goal)

Used only when Tiers 1 and 2 are both empty: every non-active room is at-or-past its goal in the conditioning direction. Plenum still needs somewhere to send the surplus air, so it picks the room with the most **headroom** before its temperature would trigger an opposite-direction cycle.

"Headroom" is how far the room can drift in the conditioning direction before the room itself would start calling for the opposite cycle:

- **Cooling:** `headroom = room.current_temp − (effective_setpoint − deadband)` — how much the room can be cooled before it crosses the "would call for heat" threshold.
- **Heating:** `headroom = (effective_setpoint + deadband) − room.current_temp` — how much the room can be warmed before it crosses the "would call for cool" threshold.

A room with zero or negative headroom is **excluded** — pushing more conditioning into it would create the very oscillation this feature is designed to prevent. The candidate(s) with the largest positive headroom win the tier.

### Tier 4 — Active rooms only (current behaviour)

If even Tier 3 yields nothing (every non-active room has zero or negative headroom), Plenum falls back to today's behaviour: only the originally-active cycle rooms stay open through the rest of the hold.

## Setpoint resolution

For each non-active room, the **effective presence setpoint** is resolved in this order:

1. The room's own presence/schedule setpoint, if configured (`Room.system_wide_temp`).
2. The thermostat's **global presence default** (`ThermostatConfig.default_temp`), used as the fallback when a room has no per-room setpoint.
3. If neither is configured, the room has no rankable goal. Tiers 1, 2, and 3 all skip it — there is nothing to compare against.

A room without a schedule and without an active presence trigger is *not* excluded from consideration: it's just an unoccupied room with no current call for conditioning. As long as it has an effective setpoint (per the resolution above), Plenum is happy to send surplus air there.

## Safety reasoning

The tier system is deliberately structured so that **Plenum never pushes a room past its own goal except when there is genuinely nowhere better to send the air**, and even then only into the room with the most headroom before it would itself trigger the opposite cycle. Specifically:

- Tiers 1 and 2 only ever choose rooms that are still moving toward their goal — no room is over-conditioned past its setpoint while a better destination exists.
- Tier 3 accepts pushing past goal, but excludes any room that is already across its opposite-direction trigger. So even when the system has nowhere ideal to dump the air, it cannot drive a room into a state where the room itself would call for the opposite cycle.
- If even Tier 3 can find no safe destination, Tier 4 falls back to the originally-active rooms — the air goes where it was already going, no new rooms are conditioned in the wrong direction.
- Vacation mode disables overflow entirely: vacation has its own hold strategy (`vacation_hvac_mode = "range" | "single"`) and we do not interfere with it.
- The candidate set is recomputed on **every tick** of the hold, not just once at hold entry. If a Tier 1 room cools past its setpoint during the hold, its vent is closed and another candidate (or none) takes its place.

These guards mean overflow conditioning cannot, by construction, create an opposite-direction cycle on the next pass.

See also [Safety features → Opposite-cycle prevention](./safety.md#opposite-cycle-prevention).

## Configuration

The behaviour is controlled per-thermostat by `ThermostatConfig.overflow_during_min_runtime`, default `True`. The toggle is exposed on the **Thermostats** page as "Redirect surplus air to other rooms during the minimum-runtime hold" — it sits next to the Min cycle runtime / Min compressor off-time fields, and is auto-disabled in the UI when Min cycle runtime is `0` (no hold ever happens, so there is nothing to redirect). Untick it to keep only the cycle's originally-active rooms open during the hold, as the system did before this feature existed.

| Knob | Where | Default | Effect |
|---|---|---|---|
| `overflow_during_min_runtime` | `ThermostatConfig` | `True` | Master switch for the tiered overflow logic. |
| `deadband` | `ThermostatConfig` | `0.5 °F` | Reused as the Tier 1 margin and the Tier 3 opposite-trigger margin. |
| `default_temp` | `ThermostatConfig` | unset | Fallback presence setpoint used when a room has no own setpoint. |
| `Room.system_wide_temp` | `Room` | unset | Per-room presence setpoint; takes priority over the thermostat default. |
| `min_cycle_runtime_min` | `ThermostatConfig` | `0` (disabled) | Without this, the hold phase never starts and overflow never runs. |

## Observability

Every overflow open/close is recorded in the cycle vent-event stream (visible on the Logs page cycle-detail view):

- `opened_overflow_hold` — reason includes the tier (`tier1`/`tier2`/`tier3`), the room's current temperature, and (for tier 3) the computed headroom.
- `closed_overflow_hold` — reason notes that the room is no longer a candidate.

An `INFO`-level event-log entry is also written when overflow first opens a non-active room, naming the tier used and the rooms involved.
