# MCP server

Plenum ships a built-in [Model Context Protocol](https://modelcontextprotocol.io) server that exposes its **entire REST API surface** as Claude-callable tools. Attach an MCP client (Claude Code, Claude Desktop, etc.) to ask natural-language questions about your setup or make configuration changes without opening the UI.

## What's exposed

The tool list is **generated from Plenum's OpenAPI spec**, so every REST endpoint is available as a tool — and any endpoint added in the future shows up automatically. That covers rooms, sensors, vents, presence sensors, schedules, thermostats and their safety config, overrides, system status, cycle/event logs, metrics, settings, and more. Each tool call is dispatched back through the running REST API, so it goes through exactly the same validation, temperature-unit conversion, and logging as the web UI — there is no separate code path to drift out of sync.

## How it runs (HTTP, on its own port)

The MCP server is served over **Streamable HTTP** on a **dedicated port (default `9099`)**, separate from the web UI. It runs inside the add-on process — you do **not** start a separate program.

Two things are required before a client can connect:

1. **Turn it on.** In the web UI, open the settings cog (⚙️) → **MCP** and toggle it on (red = off, green = on). It is **off by default** because the endpoint is unauthenticated and exposes the full write surface. The toggle takes effect immediately — no restart needed.
2. **Expose the port.** The MCP port is not published by default. How you do this depends on your install:

### Home Assistant OS / Supervised

HAOS doesn't allow direct Docker port access, so publish the port from the add-on itself:

1. Open the **Plenum** add-on → **Configuration** tab.
2. In the **Network** section, set a host port for `9099/tcp` (e.g. `9099`).
3. **Save**, then **Restart** the add-on.

### Docker (standalone)

Publish the container port when you run it:

```bash
docker run ... -p 9099:9099 ...
```

or in Compose:

```yaml
ports:
  - "9099:9099"
```

## Connecting a client

Once the toggle is on and the port is published, point your MCP client at:

```
http://<host>:9099/mcp
```

Example Claude Code / Claude Desktop config:

```json
{
  "mcpServers": {
    "plenum": {
      "url": "http://homeassistant.local:9099/mcp"
    }
  }
}
```

> ⚠️ **The MCP endpoint is unauthenticated.** Anyone who can reach the port has full read/write control of Plenum. Only expose it on a trusted network. Authentication (OAuth for MCP, plus web-UI auth) is tracked separately.

## Use cases

- "List all my rooms and show me which ones don't have a presence sensor."
- "Add an evening schedule to the bedroom rooms: 21:00 to 07:00, 68 °F, every day."
- "What were the last five cycles on the upstairs thermostat?"
- "Find all `cover.*` entities whose attributes include `current_tilt_position`" (i.e. Flair vents).

Everything the MCP server does is also available via the UI and REST API.
