# Engine Audit — Issue #32
# Cycle Engine: Full Design, Bugs, Decisions & Implementation Plan

> Companion to GitHub issue #32. Last updated: 2026-04-17.

---

## How the Engine Works Today

### Overview

One `CycleEngine` instance per thermostat zone. The `Scheduler` calls `tick()` every 60 seconds and on thermostat (`climate.*`) state-change events. Presence sensor events (`binary_sensor.*`) trigger their own handling + a tick.

### State Machine

```
IDLE → RUNNING → (all rooms at target) → TERMINATING → IDLE
RUNNING → IDLE (abort: no rooms, thermostat off, timeout)
```

`TERMINATING` is transient — it transitions to `IDLE` within the same call.

### Per-Tick Flow (`_do_tick`)

1. Expire presence holdovers and overrides
2. Compute `new_active_map` (rooms active right now, by priority: override > schedule > presence holdover)
3. If no active rooms → abort if running, run IDLE reconciliation, return
4. If thermostat unavailable → skip tick
5. If system disabled → return
6. Read `hvac_mode` via `_read_hvac_mode()`
7. If rooms changed OR state is `IDLE` → call `_start_or_update_cycle()`
8. `_monitor_rooms()` — check each room against target, close vents as rooms are satisfied
9. Check cycle timeout
10. Reconciliation pass + broadcast

### Setpoint Computation

```
cooling → setpoint = min(all active room targets) - overshoot_delta
heating → setpoint = max(all active room targets) + overshoot_delta
currently clamped to [min_setpoint, max_setpoint] — CLAMPING IS BEING REMOVED
```

### Target Check

```
cooling → at_target = avg_temp ≤ target + deadband
heating → at_target = avg_temp ≥ target - deadband
```

`temp_offset` is added to `avg_temp` before comparison.

### Room Activation Priority

1. Active override (not yet expired)
2. Matching schedule block for current local day/time
3. Presence holdover active (expires_at > now)
4. Idle (not included in cycle)

### Vent Close Logic

When a room hits target:
- `close_room_vents()` checks `min_open_vents` — if closing would drop total open count below threshold, close is deferred
- `vent_closed_at` timestamp recorded per room

### Termination

When all rooms have `vent_closed_at` set:
1. Set thermostat temperature to current ambient (stops HVAC)
2. Close cycle log, clear engine state
3. Re-open all zone vents

### Abort

- **safe_close** (thermostat unavailable): close all vents
- **normal abort** (no rooms, timeout, system disabled): open all vents, reset setpoint to ambient

---

## Bugs & Decisions

### Bug 1 — Vent actuations not logged outside dev mode

**Root cause**: Deferred closes use `log.debug`. Every vent decision should write to the event log unconditionally.

**Decision**: Promote ALL vent decisions to `info`/`warning` in the event logger — open, close, deferred close. Must fire regardless of dev mode. Dev mode only controls whether the HA call is real or simulated.

---

### Bug 2 — `heat_cool` / auto mode not supported for setpoint writes

**Root cause**: `climate.set_temperature` with only `temperature` is silently ignored by HA for `heat_cool` thermostats.

**Decision**: At cycle start, explicitly switch the thermostat to `cool` or `heat` mode by passing `hvac_mode` in the `climate.set_temperature` call. Eliminates the `heat_cool` inference path entirely.

**Decision — mode after cycle ends**: Leave the thermostat in whatever mode the cycle used (`cool` or `heat`), and set the temperature to the thermostat's current ambient reading. Since setpoint = ambient, the HVAC satisfies itself and goes idle. No mode restoration needed.

---

### Bug 3 — `min_setpoint`/`max_setpoint` fields were misused; repurposed as emergency thresholds

**What they originally did**: Clamped the *overshoot setpoint* sent to the thermostat during a cycle — a setpoint safety rail with no relation to room temperatures. This was unintentional.

**Side effect**: Ambient resets (terminate/abort) were also clamped, preventing the HVAC from stopping if ambient was outside the bounds.

**Decision**: These fields are repurposed as emergency room temperature thresholds (see Decision C below). The overshoot setpoint clamping is removed entirely. Ambient resets are no longer clamped.

---

### Bug 4 — Thermostat turned off mid-cycle not corrected

**Decision**: Reconciliation re-asserts both **hvac_mode** and **setpoint**. If the thermostat is switched off or to `heat_cool` mid-cycle, reconciliation corrects it back to the locked cycle mode + overshoot setpoint.

---

### Bug 5 — System disabled while cycle running: no abort

**Decision**: Disabling the system immediately aborts any running cycle — open all vents, reset thermostat to ambient, close cycle log.

---

### Bug 6 — Single-room zone + `min_open_vents=1` deadlock

**Decision**: If the only active room hits its target, close the vent anyway (bypass `min_open_vents`), log a warning, and terminate the cycle cleanly. `min_open_vents` must not block a clean cycle end when all rooms are satisfied.

---

### Bug 7 — `max_vent_closed_min` mid-cycle force-reopen is contrary to design intent

**Design intent**: Vents only reopen when the entire cycle terminates. Mid-cycle individual force-reopens are not wanted.

**Decision**: Keep the field but default to `0` (disabled). Remains an opt-in safety valve for HVAC pressure concerns.

---

## Decision C — Emergency Thresholds (replaces min/max setpoint clamping)

### New field meanings

| Old name | New name | Meaning |
|----------|----------|---------|
| `min_setpoint` | `emergency_heat_below` | If any room drops below this, start a heating cycle even with no active schedule |
| `max_setpoint` | `emergency_cool_above` | If any room rises above this, start a cooling cycle even with no active schedule |

**Design intent — "Away Mode"**: Emergency cycles protect the house from extremes while nobody is home. The goal is not comfort — it is preventing damage (frozen pipes, heat stress) and avoiding costly short-cycling. Target temperatures are set just inside the threshold to save energy.

**UI update**: Rename fields to "Emergency Heat Below (°F)" / "Emergency Cool Above (°F)" with helper text explaining the away-mode / safety-bounds purpose.

### Emergency Cycle Rules

**Which rooms are included**:
- Cooling: any room with `avg_temp > emergency_cool_above - deadband`
- Heating: any room with `avg_temp < emergency_heat_below + deadband`
- Rooms safely within bounds are NOT targets but benefit from open vents

**Example** (`deadband=1`, `emergency_cool_above=85`):
```
Room 1: 86°F  → past threshold              → included (triggered cycle)
Room 2: 84°F  → within deadband of ceiling  → included
Room 3: 82°F  → safely inside bounds        → NOT a target, but vents stay open
Room 4: 81°F  → safely inside bounds        → NOT a target, but vents stay open
```

**Target temperature**:
```
cooling: emergency_cool_above - max(deadband, 2)
heating: emergency_heat_below + max(deadband, 2)
```

The `max(deadband, 2)` ensures a minimum 2°F buffer inside the threshold to prevent short-cycling.

Example result: `85 - max(1, 2) = 83°F` target.

**HVAC setpoint**: Computed normally from target using `overshoot_delta` (no clamping).

**Vents**: ALL zone vents remain **open** for the entire emergency cycle. No per-room vent closing. This ensures non-target rooms are also conditioned and are less likely to trip the threshold later.

**Termination**: Standard `_is_at_target` check against the emergency target.
- In the example: `at_target = avg ≤ 83 + 1 = 84°F`
- Room 2 (84°F) is immediately satisfied; cycle ends when Room 1 also reaches 84°F.

**Room exit**: A room leaves the emergency cycle the moment `_is_at_target` is satisfied against the emergency target.

---

## Decision D — Tick Re-evaluation Covers Emergency Thresholds

On every tick (including while `IDLE`), the engine checks:

1. **Scheduled rooms**: any room in an active schedule window drifted outside target deadband → start normal cycle
2. **Emergency rooms**: any room (including unscheduled) whose temperature crossed `emergency_heat_below` or `emergency_cool_above` → start emergency cycle

The engine proactively cycles whenever anything is out of bounds, not only when a schedule is active.

---

## Decision E — Comprehensive Event Logging (all modes)

**Requirement**: Every action the engine takes must be written to the event logger (DB-backed, visible in the UI). This applies in both normal **and** dev mode. Dev mode only changes whether the HA command is real or simulated — the log entry is always written.

### Events that must always be logged

| Category | Event |
|----------|-------|
| **Cycle** | Cycle started (thermostat, mode, rooms, targets) |
| **Cycle** | Cycle terminated (all rooms at target, setpoint reset to ambient) |
| **Cycle** | Cycle aborted (reason: no rooms / timeout / system disabled / thermostat unavailable) |
| **Cycle** | Cycle timeout hit |
| **Emergency** | Emergency cycle triggered (room name, threshold crossed, actual value) |
| **Emergency** | Emergency target computed — whether `deadband` or `2°F minimum` was used, resulting target |
| **Emergency** | Emergency room satisfied (room name, current temp, target) |
| **Vent** | Vent opened (entity, room, reason) |
| **Vent** | Vent closed (entity, room, reason: at target / cycle end / abort) |
| **Vent** | Vent close deferred — min_open_vents constraint (entity, room, current open count, min required) |
| **Vent** | Vent force-reopened by max_vent_closed_min (entity, room, duration closed) |
| **Thermostat** | Setpoint written (entity, value, hvac_mode, reason: cycle start / rooms changed / reconcile) |
| **Thermostat** | Setpoint reset to ambient (entity, ambient value, reason: terminate / abort) |
| **Thermostat** | HVAC mode set (entity, mode, reason) |
| **Reconcile** | Vent drift corrected — found open, should be closed (entity) |
| **Reconcile** | Vent drift corrected — found closed, should be open (entity) |
| **Reconcile** | Thermostat setpoint drift corrected (entity, found value, expected value) |
| **Reconcile** | Thermostat mode drift corrected (entity, found mode, expected mode) |
| **Room** | Room hit target (room, avg temp, effective temp, target, offset if non-zero) |
| **Room** | Room added to running cycle (room, source, target) |
| **Room** | Room removed from running cycle (room, reason) |
| **System** | System enabled / disabled |
| **System** | Thermostat unavailable — tick skipped |
| **Presence** | Presence detected (room, sensor entity) |
| **Presence** | Presence holdover expired (room) |

### Log levels

| Level | When |
|-------|------|
| `info` | Normal expected events (cycle start/end, vent open/close, setpoint set) |
| `warning` | Unexpected or safety-triggered events (drift corrected, min_open_vents bypass, emergency cycle, thermostat unavailable) |
| `error` | HA call failed |

---

## All Scenarios

### Cycle Lifecycle

| # | Scenario | Status |
|---|----------|--------|
| 1 | Normal cooling cycle — schedule-based, vents close sequentially | ✅ works |
| 2 | Normal heating cycle | ✅ works |
| 3 | Cycle start skipped — all rooms within deadband | ✅ works |
| 4 | Cycle timeout | ✅ works |

### Room Changes Mid-Cycle

| # | Scenario | Status |
|---|----------|--------|
| 5 | Room added mid-cycle (new schedule block) | ✅ works |
| 6 | Room added mid-cycle (presence trigger) | ✅ works |
| 7 | Room removed mid-cycle (schedule ends) | ✅ works |
| 8 | Room removed mid-cycle (presence holdover expires) | ✅ works |
| 9 | All rooms removed mid-cycle → abort | ✅ works |
| 10 | Room's vent closed (at target), room removed — vent reopens | ✅ works |

### Thermostat / HVAC State

| # | Scenario | Status |
|---|----------|--------|
| 11 | Thermostat unavailable — tick skipped, cycle survives | ✅ works |
| 12 | Thermostat turned off mid-cycle — reconciliation re-asserts mode + setpoint | 🔴 needs fix (Bug 4) |
| 13 | `heat_cool` thermostat — engine switches to explicit `heat`/`cool` at cycle start | 🔴 needs fix (Bug 2) |
| 14 | Cycle ends — thermostat left in cycle mode, temperature set to ambient | 🔴 needs fix (Bug 2 + Bug 3) |
| 15 | Single-direction thermostat — standard setpoint flow | ✅ works |
| 16 | External actor changes thermostat setpoint → reconciliation re-asserts | ✅ works |

### Safety / Bounds

| # | Scenario | Status |
|---|----------|--------|
| 17 | `min_open_vents` deferred close — deferral logged | 🔴 needs fix (Bug 1 + Decision E) |
| 18 | `min_open_vents` + last room at target — bypass and terminate | 🔴 needs fix (Bug 6) |
| 19 | `max_vent_closed_min` force-reopen — keep as 0-disabled opt-in | 🟡 default must be 0 (Bug 7) |
| 20 | Overshoot setpoint clamping removed | 🔴 needs fix (Bug 3) |
| 21 | Ambient reset (terminate/abort) not clamped | 🔴 needs fix (Bug 3) |

### Emergency Cycles (new)

| # | Scenario | Status |
|---|----------|--------|
| 22 | Room exceeds cooling ceiling → emergency cycle, all vents open | 🔴 needs implementation |
| 23 | Room drops below heating floor → emergency cycle, all vents open | 🔴 needs implementation |
| 24 | Rooms within deadband of threshold included in emergency cycle | 🔴 needs implementation |
| 25 | Emergency target = `threshold - max(deadband, 2)`, buffer choice logged | 🔴 needs implementation |
| 26 | Emergency room exits when `_is_at_target` satisfied vs emergency target | 🔴 needs implementation |
| 27 | No per-room vent closing during emergency cycle | 🔴 needs implementation |
| 28 | UI labels updated: "Emergency Heat Below" / "Emergency Cool Above" | 🔴 needs implementation |

### Priority / Activation Sources

| # | Scenario | Status |
|---|----------|--------|
| 29 | Override active — takes priority over schedule | ✅ works |
| 30 | Override expires while cycle running | ✅ works |
| 31 | Presence holdover activates a room | ✅ works |
| 32 | Presence holdover expires while cycle running | ✅ works |
| 33 | Overnight schedule (spans midnight) | ✅ works |
| 34 | Multiple overlapping schedules — tiebreak by earliest start_time | ✅ works |

### System State

| # | Scenario | Status |
|---|----------|--------|
| 35 | System disabled — ticks skip | ✅ works |
| 36 | System disabled while cycle running → immediate abort | 🔴 needs fix (Bug 5) |
| 37 | Developer mode — HA commands dry-run but events still logged | 🔴 needs fix (Decision E) |

### Edge Cases

| # | Scenario | Status |
|---|----------|--------|
| 38 | Mixed rooms (some heat, some cool) — majority wins | ✅ works |
| 39 | Room has no sensors — skipped, does not block termination | ✅ works |
| 40 | Reconciliation: vent opened externally → re-close | ✅ works |
| 41 | Reconciliation: vent closed externally → re-open | ✅ works |
| 42 | IDLE reconciliation: vent found closed → re-open | ✅ works |
| 43 | Scheduled room drifts after cycle ends → new cycle on next tick | ✅ works |
| 44 | Unscheduled room drifts beyond emergency threshold → emergency cycle | 🔴 needs implementation |
| 45 | Emergency cycle active + room has scheduled target — scheduled target takes precedence if stricter | 🔴 edge case, needs implementation |

---

## Implementation Checklist

### `ha_client.py`
- [ ] `set_thermostat_temperature` accepts `hvac_mode` param and passes it in the HA service call
- [ ] All HA calls log to event logger regardless of dev mode

### `vent_controller.py`
- [ ] Promote deferred close from `log.debug` to event logger `warning` (room, vent, open count, min required)
- [ ] All `open_room_vents` / `close_room_vents` / `close_all_zone_vents` log unconditionally with reason
- [ ] `check_max_closed_duration` force-reopen logs duration closed and vent entity

### `cycle_engine.py`
- [ ] Remove `[min_setpoint, max_setpoint]` clamp from `_set_thermostat_setpoint`
- [ ] Remove clamp from ambient resets in `_terminate_cycle` and `_abort_cycle`
- [ ] Pass `hvac_mode` when calling `set_thermostat_temperature` at cycle start
- [ ] Reconciliation re-asserts `hvac_mode` in addition to setpoint; logs both corrections
- [ ] System-disable path immediately aborts running cycle + logs reason
- [ ] Single-room + `min_open_vents` bypass: close vent, log warning, terminate
- [ ] Add `_is_emergency_cycle: bool` flag to engine state
- [ ] Emergency room detection in `_do_tick`: rooms within `deadband` of threshold
- [ ] Emergency target computation: `threshold - max(deadband, 2)` — log which buffer was used and resulting target
- [ ] Log emergency cycle trigger: room name, threshold crossed, actual value
- [ ] Emergency cycle: skip per-room vent closing in `_monitor_rooms`
- [ ] Emergency cycle: all zone vents forced open at cycle start
- [ ] Emergency cycle termination: temperature-only check; log each room as it satisfies target
- [ ] Every `_logger.log` call includes structured metadata (entity IDs, numeric values, reason strings)

### `models.py`
- [ ] Rename `min_setpoint` → `emergency_heat_below`
- [ ] Rename `max_setpoint` → `emergency_cool_above`
- [ ] Add DB migration

### Frontend — Thermostat settings UI
- [ ] Rename "Min Setpoint" → "Emergency Heat Below (°F)"
- [ ] Rename "Max Setpoint" → "Emergency Cool Above (°F)"
- [ ] Add helper text explaining away-mode / emergency purpose
