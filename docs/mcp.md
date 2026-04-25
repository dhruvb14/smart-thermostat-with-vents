# MCP server

Plenum ships an optional [Model Context Protocol](https://modelcontextprotocol.io) server that exposes most of its read and write surface as Claude-callable tools. Run it alongside Claude Code (or another MCP client) to ask natural-language questions about your setup or make configuration changes without opening the UI.

## What's exposed

| Module | Tools |
|---|---|
| **Rooms** | List rooms, read a specific room, create rooms, add/remove temperature sensors, vents, and presence sensors. |
| **Schedules** | List, create, update, and delete schedule blocks. Times are in `HH:MM` local format. |
| **Thermostats** | Set per-thermostat safety configuration (setpoint bounds, deadband, overshoot, timeouts, etc.). |
| **Status** | Read the current system status — active overrides, presence holdover state, matching schedules. Read recent cycle logs. |
| **HA entities** | List Home Assistant entities filtered by domain (`cover`, `climate`, `sensor`, `binary_sensor`, …) or attribute. Useful for onboarding — Claude can find the right entity IDs for you. |

## How it runs

The MCP server uses stdio transport and shares the same SQLite database as the main add-on. It does not open its own HA WebSocket — it reads and writes config only. Run it from the `smart_vent/` package:

```bash
python -m backend.mcp_server
```

Environment variables mirror the add-on: `DATA_DIR` (where `app.db` lives), `HA_URL`, `HA_TOKEN`.

## Use cases

- "List all my rooms and show me which ones don't have a presence sensor."
- "Add an evening schedule to the bedroom rooms: 21:00 to 07:00, 68 °F, every day."
- "What were the last five cycles on the upstairs thermostat?"
- "Find all `cover.*` entities whose attributes include `current_tilt_position`" (i.e. Flair vents).

The MCP server is optional — everything it does is available via the UI and REST API.
