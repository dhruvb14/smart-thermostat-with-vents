# Plenum documentation

High-level guide to what Plenum does and how the pieces fit together. For install instructions and quick-start, see the [top-level README](../README.md).

## Feature guides

- [Rooms & zones](./rooms-and-zones.md) — how rooms map to thermostats and what gets configured per room
- [Cycle engine](./cycle-engine.md) — the HVAC cycle state machine: what runs every 60s tick
- [Overflow conditioning](./overflow-conditioning.md) — where surplus air goes during the minimum-runtime hold
- [Vent control methods](./vent-control.md) — the four ways Plenum can drive a `cover.*` entity
- [Thermostat settings](./thermostat-settings.md) — setpoint bounds, deadband and the room/schedule overrides that inherit from it, overshoot, timeouts, safety limits
- [Safety features](./safety.md) — short-cycle protection, outdoor-temperature cooling lockout, equipment-protection limits
- [Schedules](./schedules.md) — time blocks, overnight ranges, priority rules, per-block deadband override
- [Presence & motion](./presence.md) — motion-triggered activation and holdover
- [Temperature holds](./temperature-holds.md) — hold one room at an exact temperature for 1–8 hours, overriding schedules and presence
- [Pre-cool / pre-heat](./precool-presence.md) — let a room coast to target on outside air instead of running HVAC for presence
- [Eco Mode](./eco-mode.md) — outdoor-temperature-compensated setpoint drift (relax targets when it's extreme outside)
- [System modes](./system-modes.md) — the System On/Off toggle and Dev Mode
- [Observability](./observability.md) — dashboard, event logs, cycle history, WebSocket
- [Metrics & analytics](./metrics.md) — `/metrics` page charts, outside-temperature correlation, CSV export, live HA sensor endpoint
- [Backup & restore](./backup-restore.md) — download/upload the SQLite database
- [Authentication](./auth.md) — the trust model: ingress is always trusted; the direct web-UI and MCP ports can require login / scoped bearer tokens
- [MCP server](./mcp.md) — Claude-callable tools over the add-on
- [MQTT interface](./mqtt.md) — drive Plenum from Home Assistant automations, with controls exposed as native HA entities
- **API Documentation** — Interactive Swagger UI available at `/api/docs`

## Conventions

- All temperatures are stored and displayed in **°F**. Sensors reporting °C are converted on ingest.
- All times are evaluated in the timezone configured in the add-on's **Configuration** tab (see the [top-level README](../README.md#timezone-configuration)).
- Entity IDs follow Home Assistant's `domain.object_id` format. Plenum only drives `cover.*` (vents), `climate.*` (thermostats), reads from `sensor.*` (temperature), and `binary_sensor.*` (presence).
