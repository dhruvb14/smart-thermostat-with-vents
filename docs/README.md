# Plenum documentation

High-level guide to what Plenum does and how the pieces fit together. For install instructions and quick-start, see the [top-level README](../README.md).

## Feature guides

- [Rooms & zones](./rooms-and-zones.md) — how rooms map to thermostats and what gets configured per room
- [Cycle engine](./cycle-engine.md) — the HVAC cycle state machine: what runs every 60s tick
- [Vent control methods](./vent-control.md) — the four ways Plenum can drive a `cover.*` entity
- [Thermostat settings](./thermostat-settings.md) — setpoint bounds, deadband, overshoot, timeouts, safety limits
- [Schedules](./schedules.md) — time blocks, overnight ranges, priority rules
- [Presence & motion](./presence.md) — motion-triggered activation and holdover
- [System modes](./system-modes.md) — the System On/Off toggle and Dev Mode
- [Observability](./observability.md) — dashboard, event logs, cycle history, WebSocket
- [Metrics & analytics](./metrics.md) — `/metrics` page charts, outside-temperature correlation, CSV export, live HA sensor endpoint
- [Backup & restore](./backup-restore.md) — download/upload the SQLite database
- [MCP server](./mcp.md) — Claude-callable tools over the add-on
- **API Documentation** — Interactive Swagger UI available at `/api/docs`

## Conventions

- All temperatures are stored and displayed in **°F**. Sensors reporting °C are converted on ingest.
- All times are evaluated in the timezone configured in the add-on's **Configuration** tab (see the [top-level README](../README.md#timezone-configuration)).
- Entity IDs follow Home Assistant's `domain.object_id` format. Plenum only drives `cover.*` (vents), `climate.*` (thermostats), reads from `sensor.*` (temperature), and `binary_sensor.*` (presence).
