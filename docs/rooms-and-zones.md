# Rooms & zones

A **zone** is everything a single thermostat controls. A **room** belongs to exactly one zone and is the unit of scheduling and presence control.

## What a room carries

Every room has:

- A **name** and a **thermostat** (`climate.*`) it belongs to.
- Zero or more **temperature sensors** (`sensor.*`). Their values are averaged to determine "room temperature". Sensors reporting °C are converted to °F automatically.
- An **Include thermostat sensor** toggle — when on, the thermostat's own ambient reading is averaged in alongside the room sensors.
- Zero or more **vents** (`cover.*`). Each vent stores its own **control method** (see [Vent control methods](./vent-control.md)).
- Zero or more **presence sensors** (`binary_sensor.*`) plus a **presence holdover** (hours) and a **presence target temp**. See [Presence & motion](./presence.md).
- A **temperature offset** (°F) added to the measured average before comparing to target. Use this to compensate for post-vent-close drift — e.g. set `+3` if your room always ends up 3° cooler than target after the vent closes.
- An optional **deadband override** (± °F, 0–10) that replaces the thermostat's [Deadband](./thermostat-settings.md#deadband-inheritance) for this room only — the tolerance around target within which the room calls for nothing. Leave it blank to inherit the thermostat's value. A single [schedule block](./schedules.md#deadband-override) can override this in turn, but only for the hours that block is what has the room active.
- Optional **pre-cool / pre-heat** settings that let the room coast to its presence target on outside air instead of running HVAC (enable, when-to-apply, minimum outside difference, widened deadband). Requires an outside temperature sensor. See [Pre-cool / pre-heat](./precool-presence.md).

## Multiple rooms per zone

Several rooms sharing one thermostat is the primary use case. The cycle engine coordinates them: it picks a heating/cooling direction based on which rooms need what, opens vents for the participating rooms, and closes each room's vents independently as it reaches target. The cycle ends once every active room has either hit target or timed out.

## Multiple vents per room

A room can have any number of vents. They're opened together when the room participates in a cycle and closed together when the room hits target. Per-thermostat safety limits (**min open vents**, **max vent closed**) still apply — see [Thermostat settings](./thermostat-settings.md).
