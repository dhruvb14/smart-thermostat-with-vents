# Schedules

A **schedule** is a time block that sets a target temperature for a room on specific days. When the current time falls inside a matching block, the room activates at that target.

## What a block defines

- **Days of week** — any subset of Mon–Sun.
- **Start time** and **end time** — local-time `HH:MM` values (evaluated in the add-on's configured timezone).
- **Target temperature** (°F).

## Overnight blocks

A block whose end time is earlier than its start time (e.g. 21:00 → 07:00) spans midnight. Internally it resolves to two intervals — one for the evening of the selected day and one for the morning of the next calendar day — so matching works cleanly around the midnight boundary.

## Priority and overlaps

Room activation priority, highest first:

1. **Manual override** (set via the UI)
2. **Schedule**
3. **Presence holdover**

Within schedules, **overlapping blocks on the same room are rejected at save time** — you can't have two blocks competing for the same moment. If somehow two match (e.g. near a DST boundary), the earliest start time wins.

## Timezone behavior

Schedule times are local-time, evaluated in the timezone configured on the add-on's **Configuration** tab. If you change the timezone, existing blocks keep their literal `HH:MM` values — they just now mean those times in the new zone. See the [top-level README](../README.md#timezone-configuration) for how to set this.
