# Presence & motion

Presence sensors let a room activate on demand — when someone walks in, the HVAC warms (or cools) the room to a configured target and holds it there until the space has been quiet for a configured holdover period.

## How a presence-triggered room activates

1. Any configured `binary_sensor.*` on the room fires (`on` state).
2. The room enters the presence-active state and starts a **holdover timer**. The target is:
   - the **room-level presence temp** (`system_wide_temp`) if set, otherwise
   - the **thermostat-level Default presence temp**, otherwise
   - the room is logged as "skipped" and stays idle.
3. Each additional motion event **resets** the holdover expiry. The room stays active until the timer runs out.
4. When the timer expires, the room falls back to its next priority (schedule, or idle).

## Holdover duration

Configured per room in hours (default `2.0`). A value of `0` disables presence activation for that room.

## Priority

Presence is lower priority than a matching schedule or a manual override (see [Schedules](./schedules.md)). If a schedule block and presence both apply, the schedule wins — presence won't re-activate the room until the schedule exits.

## Why holdover lives in the DB

Holdover expiry is persisted, so an add-on restart or a crash doesn't lose presence state. A room active at the moment of restart stays active through the boot, assuming the holdover hasn't expired.
