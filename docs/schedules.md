# Schedules

A **schedule** is a time block that sets a target temperature for a room on specific days. When the current time falls inside a matching block, the room activates at that target.

## What a block defines

- **Name** (optional) — a label for the block, so the list says what it is *for* and not only when it runs. Blank leaves it unnamed. See [Naming a block](#naming-a-block).
- **Days of week** — any subset of Mon–Sun.
- **Start time** and **end time** — local-time `HH:MM` values (evaluated in the add-on's configured timezone).
- **Target temperature** (°F).
- **Deadband override** (optional) — how far the room may drift from that target before it calls for heating or cooling, for this block's hours only. Blank inherits. See [Deadband override](#deadband-override).

## Naming a block

A block can carry an optional display name of up to 64 characters — *Weekday night setback*, *Guest stay*, *Nursery nap*. It is shown in the **Name** column of the Schedules table; a block with no name reads *Unnamed*. Nothing else changes: the name is a label the engine never reads.

**A name is not an identifier.** Two blocks may share one, and a block's ID never changes when you rename it. Every row therefore carries an **ID** chip next to the name — hover it to see the block's ID, named or not. That ID is what addresses the block directly over the [REST API](../README.md) and [MCP](./mcp.md), so it stays one hover away rather than something you have to read out of the database.

Copying a block to other rooms copies its name too: a name describes what the block is for, which is exactly what should replicate. Names are per-room labels, so the same *Night setback* in four rooms is the intent, not a collision.

## Overnight blocks

A block whose end time is earlier than its start time (e.g. 21:00 → 07:00) spans midnight. Internally it resolves to two intervals — one for the evening of the selected day and one for the morning of the next calendar day — so matching works cleanly around the midnight boundary.

## Priority and overlaps

Room activation priority, highest first:

1. **Manual override** (set via the UI)
2. **Schedule**
3. **Presence holdover**

Within schedules, **overlapping enabled blocks on the same room are rejected at save time** — you can't have two blocks competing for the same moment. A disabled block does not reserve its slot and is not checked, which is what makes the [guest room pattern](#guest-room-pattern) work. If somehow two match (e.g. near a DST boundary), the earliest start time wins.

## Deadband override

The block editor's **Temperature drift** control has two settings: *Use the room's normal deadband* (the default) or *Override deadband*, which takes a ± value bounded to 0–10 °F.

**The block's value replaces the room's — it is not added to it.** A room whose deadband is ±3 °F, with a block set to ±1 °F, drifts ±1 °F while that block runs, not ±4 °F. So the control widens *or* narrows: a night block can loosen a room nobody is using, and a nursery block can hold one tighter than the room's own setting.

The band resolves most specific first:

| Level | Where it is set | Applies when |
|---|---|---|
| **Schedule block** | *Temperature drift* in the block editor | Set on the block, **and** that block is what has the room active |
| **Room** | *Deadband override* in Room settings | The block sets none, or no block is active |
| **Thermostat** | *Deadband* on the Thermostats page | Neither override is set |

Unset at a level means "inherit the next one down", so a room with no override and a block with no override behave exactly as they did before either field existed. The Schedules table marks a block that carries one with a `±3°F drift` badge, so wide-band blocks are visible without opening the editor.

### The band belongs to the activation, not the room

A block's band applies only while that block is the room's **active source**. Activation priority is manual override → schedule → presence holdover (see [Priority and overlaps](#priority-and-overlaps)), and only the schedule source carries a block's number. If a manual override or a presence holdover is what has the room active, the block's band is ignored and the room → thermostat chain applies — even during the block's own hours. The same is true of a room that is not active at all: it has no matching block by definition, so overflow conditioning and the vent safety sweep use the room → thermostat chain.

### What a wider band does

A wider band means the room **waits longer before calling for HVAC**. With a 70 °F target, a 0.5 °F band asks for heat below 69.5 °F; a 3 °F band does not ask until 67 °F.

It does not change where the room ends up. The deadband gates cycle **start** and mid-cycle **join** only — never completion. Once a cycle is running, a room's vents close at its exact target (see [Cycle engine](./cycle-engine.md)), so a 3 °F-band room still gets heated to 70 °F. It just gets there less often, which is the point in a room nobody is using.

### Guest room pattern

Overlap is enforced only between **enabled** blocks, so the same window can hold two blocks as long as one of them is parked. Keep both settings for a spare room and flip which one is live:

1. A **night block** — 22:00–07:00, 66 °F, 3 °F drift. The cheap setting for an empty room.
2. A **guest block** — the same 22:00–07:00 window, 68 °F, 0.5 °F drift.

Disable the night block first (a new block is created enabled, and an enabled block would be rejected as an overlap), then add the guest block. When the room empties, disable the guest block and re-enable the night one. Neither is ever deleted; only the **Enable** / **Disable** toggle moves.

Pair the guest block with an **Auto-disable at** expiry set to the end of the stay and it parks itself: the expiry sweep disables the block once its current run has finished, and never deletes it. Re-enabling the night block is still a manual step — expiry only ever disables.

The whole flip is also reachable over [MCP](./mcp.md): `create_schedule` takes `enabled` and `expires_at`, and `update_schedule` can park or re-arm a block (`enabled`), set an expiry, or drop one (`clear_expires_at` — needed because an omitted argument and a null one are indistinguishable there). Both refuse a block that would overlap a live one, so an assistant cannot produce a state the UI would have rejected. Both also take `name`, with `clear_name` to return a block to unnamed — so "call the 66 °F one *Night setback* and the other one *Guest stay*" is a single request, and `list_schedules` reports each block's name alongside its ID.

## Timezone behavior

Schedule times are local-time, evaluated in the timezone configured on the add-on's **Configuration** tab. If you change the timezone, existing blocks keep their literal `HH:MM` values — they just now mean those times in the new zone. See the [top-level README](../README.md#timezone-configuration) for how to set this.
