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
**bearer token**. Mint one in the web UI: **Settings** page → **MCP access
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

## Cloud connectors & OIDC clients (an OAuth2 auth proxy)

The bearer-token flow above works for any client that lets you set a static
`Authorization` header — a local `mcpServers` config, a script, `curl`. Some
clients don't work that way. **Hosted "custom connectors"** — Claude's cloud/web
custom connectors, OpenAI's connectors, and other remote MCP providers — expect
the server to speak the MCP **OAuth 2.1 authorization flow** (metadata discovery,
dynamic client registration, an interactive login) and to be reachable at a
public HTTPS URL. Plenum only issues **static bearer tokens**, so those clients
cannot attach to `/mcp` directly.

The bridge is to put an **OIDC-aware auth proxy in front of Plenum's MCP port**.
The proxy presents the OAuth/OIDC front end the connector expects, authenticates
each user against *your own* identity provider (Authelia, Authentik, Keycloak,
Google, Microsoft Entra ID, …), and — once a user is allowed through — forwards
the request upstream to Plenum's `/mcp` with a static bearer token attached:

```
Claude / OpenAI connector ──OAuth 2.1 / OIDC──▶ auth proxy ──Bearer token──▶ Plenum :9099 /mcp
      (interactive login via your IdP)                     (injected upstream)
```

> **⚠️ Third-party software — not part of Plenum, not endorsed, no affiliation.**
> The example below uses [`sigbit/mcp-auth-proxy`](https://github.com/sigbit/mcp-auth-proxy),
> a third-party project and container image. **Plenum's maintainer does not
> endorse it, is not affiliated with the `sigbit/mcp-auth-proxy` project or its
> image in any way, and has not vetted or hardened it.** It is shown here only as
> one personal setup that happens to work — *not* a recommendation, and it is
> neither shipped nor supported by Plenum. Any OAuth2/OIDC proxy that can inject
> an upstream bearer token will do the same job. If you run one, the security
> review is yours: read its source, pin the image to a digest, and keep it
> patched.

### Example: `mcp-auth-proxy` via Docker Compose

```yaml
services:
  plenum-mcp-auth-proxy:
    container_name: plenum-mcp-auth
    image: ghcr.io/sigbit/mcp-auth-proxy:latest
    mem_limit: 128m
    ports:
      - "6479:80"
    environment:
      - EXTERNAL_URL=https://plenum-mcp.your-fqdn.com
      - TLS_ACCEPT_TOS=false
      - NO_AUTO_TLS=true
      - OIDC_CONFIGURATION_URL=https://auth.your-fqdn.com/.well-known/openid-configuration
      - OIDC_CLIENT_ID=XXXXXXXX-1XX8-4XX3-bXX7-aXXXXXXXXXXc
      - OIDC_CLIENT_SECRET=REPLACE_WITH_YOUR_OIDC_CLIENT_SECRET
      - OIDC_SCOPES=openid,email,groups
      - OIDC_ALLOWED_USERS_GLOB=*
      - PROXY_BEARER_TOKEN=REPLACE_WITH_YOUR_MINTED_TOKEN
      - TRUSTED_PROXIES=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
    volumes:
      - ./data:/data
    command: ["http://HOME-ASSISTANT-IP:9099/mcp"]
    restart: always
```

What the pieces do (consult the proxy's own docs for the authoritative list):

| Setting | What it does |
|---|---|
| **`EXTERNAL_URL`** | Public HTTPS URL your connector points at. Must match the address your reverse proxy serves. |
| **`TLS_ACCEPT_TOS` / `NO_AUTO_TLS`** | Both are set here to **disable** the proxy's built-in ACME/Let's Encrypt, because TLS is terminated by a separate reverse proxy (below). Flip them if you want the proxy to obtain its own certificate instead. |
| **`OIDC_CONFIGURATION_URL`** | Your identity provider's OpenID Connect discovery document (`…/.well-known/openid-configuration`). |
| **`OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET`** | The OAuth client you register with that IdP for this proxy. |
| **`OIDC_SCOPES`** | Scopes requested at login. |
| **`OIDC_ALLOWED_USERS_GLOB`** | Which authenticated users may pass. `*` allows **everyone** your IdP authenticates — narrow this to specific users/emails/groups in production. |
| **`PROXY_BEARER_TOKEN`** | The **Plenum-minted MCP token** the proxy injects on every upstream request. Mint it in Plenum (**Settings** → **MCP access tokens**) and paste it here. |
| **`TRUSTED_PROXIES`** | CIDRs the proxy trusts for `X-Forwarded-*` headers (your reverse proxy / LAN). |
| **`command: ["http://HOME-ASSISTANT-IP:9099/mcp"]`** | The **upstream** MCP endpoint the proxy forwards to — Plenum's host + published `9099/tcp` + `/mcp`. |
| **`ports: "6479:80"`** | Host port your reverse proxy targets. |

> **Reverse proxy / TLS is out of scope.** This example assumes you *already*
> have a reverse proxy terminating TLS for `https://plenum-mcp.your-fqdn.com` and
> forwarding it to host port `6479`. Standing up that proxy and its certificate
> is your responsibility and is not covered here — any mainstream reverse proxy
> (Nginx Proxy Manager, Traefik, Caddy, or Home Assistant's own) will do.

### Wiring it up

1. **Turn MCP on in Plenum** (settings cog (⚙️) → **MCP**) and publish `9099/tcp`
   only on the internal network the proxy can reach. **Do not expose `9099`
   publicly** — the auth proxy is meant to be the *only* public door; firewall
   the raw port off the internet.
2. **Keep `require_auth` on.** The proxy authenticates *who* connects; Plenum's
   own bearer check is still what protects `/mcp`, so leave it enabled and give
   the proxy a real token.
3. **Mint the token with least privilege.** Every user allowed through the proxy
   shares the single `PROXY_BEARER_TOKEN`, so they all inherit its scope. Pick the
   smallest scope you need — `read` for inspection-only, `write` to allow config
   changes — rather than `destructive`. The OIDC allowlist gates *who* connects;
   the token scope gates *what* they can do, and it is the same for everyone
   behind the proxy.
4. **Register an OAuth client** with your identity provider and fill in the
   `OIDC_*` values.
5. **Add the server in your connector** (Claude custom connector, OpenAI, or
   another OAuth-capable MCP client) by its public URL. The connector runs the
   OAuth login against your IdP; on success the proxy forwards the session to
   Plenum's `/mcp`. The exact path to enter (e.g. `…/mcp`) is dictated by the
   proxy — check its documentation.

## Use cases

- "List all my rooms and show me which ones don't have a presence sensor."
- "Add an evening schedule to the bedroom rooms: 21:00 to 07:00, 68 °F, every day."
- "What were the last five cycles on the upstairs thermostat?"
- "Find all `cover.*` entities whose attributes include `current_tilt_position`" (i.e. Flair vents).

Everything the MCP server does is also available via the UI and REST API.
