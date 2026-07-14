# MCP server

Plenum ships a built-in [Model Context Protocol](https://modelcontextprotocol.io) server that exposes its **entire REST API surface** as Claude-callable tools. Attach an MCP client (Claude Code, Claude Desktop, etc.) to ask natural-language questions about your setup or make configuration changes without opening the UI.

## What's exposed

The tool list is **generated from Plenum's OpenAPI spec**, so every REST endpoint is available as a tool — and any endpoint added in the future shows up automatically. That covers rooms, sensors, vents, presence sensors, schedules, thermostats and their safety config, overrides, system status, cycle/event logs, metrics, settings, and more. Each tool call is dispatched back through the running REST API, so it goes through exactly the same validation, temperature-unit conversion, and logging as the web UI — there is no separate code path to drift out of sync.

## How it runs (HTTP, on its own port)

The MCP server is served over **Streamable HTTP** on a **dedicated port (default `9099`)**, separate from the web UI. It runs inside the add-on process — you do **not** start a separate program.

Two things are required before a client can connect:

1. **Turn it on.** In the web UI, open the settings cog (⚙️) → **MCP** and toggle it on (red = off, green = on). It is **off by default** because it exposes the full write surface. The toggle takes effect immediately — no restart needed.
2. **Expose the port.** The MCP server listens on its own port; how it's published depends on your install:

### Home Assistant OS / Supervised

HAOS doesn't allow direct Docker port access, so the port is published from the add-on itself:

1. Open the **Plenum** add-on → **Configuration** tab.
2. In the **Network** section, find the `9099/tcp` row and set a host port (e.g. `9099`). Leave it blank to keep the port unpublished.
3. **Save**, then **Restart** the add-on.

> **Don't see a `9099/tcp` row?** Home Assistant re-reads an add-on's `config.yaml` (including its ports) whenever the add-on's **version changes and you press Update** — no uninstall/reinstall needed. `9099/tcp` was added in **v0.22.1**, so if Plenum shows an available update, apply it and the row appears automatically. The one thing that does *not* trigger a reload is re-pulling the image at the *same* version — the version has to change. (This is a Supervisor behavior, not a per-port setting: the row shows for any declared port, and a blank/`null` mapping still shows an empty row you can fill in.)

(Publishing the port is safe on its own — the MCP endpoint returns `503` until you also flip the toggle in step 1.)

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
      "url": "http://homeassistant.local:9099/mcp",
      "headers": { "Authorization": "Bearer <your-token>" }
    }
  }
}
```

## Authentication

When `require_auth` is on (the default), every `/mcp` request must present a
**bearer token**. Mint one in the web UI: **Thermostats** page → **MCP access
tokens** → choose a scope and label → **Mint token**. The secret is shown
**once** — copy it into your client's `Authorization: Bearer <token>` header (as
above). Only a SHA-256 hash of the token is stored, so a database backup can't
leak it.

Tokens are **scoped** — `read` (inspect only), `write` (also
create/update), or `destructive` (also restart/restore/backup and token
management). A call that exceeds the token's scope is rejected with `403`. See
[`auth.md`](auth.md) for the full trust model.

> If `require_auth` is set to `false` (legacy open mode) the MCP endpoint is
> **unauthenticated** — anyone who can reach the published port has full
> read/write control. Only do this on a trusted network.

## Use cases

- "List all my rooms and show me which ones don't have a presence sensor."
- "Add an evening schedule to the bedroom rooms: 21:00 to 07:00, 68 °F, every day."
- "What were the last five cycles on the upstairs thermostat?"
- "Find all `cover.*` entities whose attributes include `current_tilt_position`" (i.e. Flair vents).

Everything the MCP server does is also available via the UI and REST API.
