---
name: plenum-auth-campaign
description: Executable, decision-gated campaign plan for issue #373 — authentication for the Plenum web UI (port 8099) and MCP server (port 9099). Load when working on #373 or any auth-adjacent change; adding login, sessions, bearer tokens, or OAuth; deciding how to detect "came via HA ingress" vs direct-port access without spoofing; securing /api/restart, /api/backup, /api/restore, /ws, or /mcp; or reviewing a PR that touches request identity or trust boundaries.
---

# Plenum auth campaign (#373)

**STATUS: CANDIDATE campaign plan, not decreed scope.** Issue #373 is open and
its design questions are explicitly undecided by the owner. Every choice below
that the owner has not ratified is labeled **OPEN DECISION** with a
recommendation. Do not treat a recommendation as approval. Phase 1 exists
precisely to get the decisions ratified before code is written.

**Audience**: a session (human or model) advancing #373 without supervision.
Work phase by phase. Each phase has an entry gate — do not skip gates.

When NOT to use this skill:
- General change/PR gating → `plenum-change-control` (this campaign routes all
  merges through it; gates are not restated here).
- Test-writing mechanics, coverage gates → `plenum-validation-and-qa`.
- Running the stack / attaching an MCP client / port mapping → `plenum-run-and-operate`.
- Where a config knob lives / add-a-knob parity checklist → `plenum-config-and-flags`.
- Why the MCP loopback design exists → `plenum-architecture-contract`; the
  #385/#387 "default-mapped MCP port" revert story → `plenum-failure-archaeology`.

Jargon used below: **ingress** = Home Assistant's built-in reverse proxy that
serves an add-on's UI inside the HA frontend, authenticated by the user's HA
session — the add-on itself never sees the HA login. **Direct port** = a
container port the user has published on the host via the add-on's Network
panel (`ports:` in `config.yaml`); traffic there bypasses HA entirely.
**Supervisor** = the HA process that manages add-ons and offers internal APIs
at `http://supervisor/` to containers holding `SUPERVISOR_TOKEN`.

---

## 1. Verified current state (as of 2026-07-05, v0.22.1 — re-verify per §9)

All facts below were read from the repo. File paths are repo-root-relative.

| Fact | Evidence |
|---|---|
| Web UI/API: aiohttp binds `0.0.0.0:8099` | `smart_vent/backend/main.py` (`web.TCPSite(runner, "0.0.0.0", PORT)`, `PORT` default 8099) |
| MCP: uvicorn/Starlette binds `0.0.0.0:9099` | `main.py` `_start_mcp_server` (`uvicorn.Config(..., host="0.0.0.0", port=MCP_PORT)`, default 9099) |
| **Both listeners bind all interfaces** → the ingress proxy and direct-port clients hit the *same* sockets. Port/interface alone cannot distinguish them today. | same two lines |
| Only middleware in the app is `security_headers_middleware` (headers only, no identity) | `main.py` `web.Application(middlewares=[security_headers_middleware])` |
| **Zero auth code exists.** No route reads `Authorization`, cookies, or any `X-Ingress`/`X-Hass`/`X-Remote` header | `grep -rn "X-Ingress\|X-Hass\|X-Remote\|Authorization\|Cookie" smart_vent/backend --include='*.py'` → only HA-client outbound auth |
| 73 REST routes, all anonymous; includes `POST /api/restart`, `GET /api/backup` (downloads the whole SQLite DB), `POST /api/restore` (replaces the DB) | `smart_vent/backend/api/routes.py` (~lines 2004, 2423, 2464 — drift-prone, grep instead) |
| `/ws` WebSocket and `/api/docs/` Swagger UI are also anonymous | `main.py` (`app.router.add_get("/ws", ...)`, `setup_openapi(...)`) |
| MCP endpoint `/mcp` on 9099: bare `/mcp` → **307** to `/mcp/`; when `mcp_enabled` is off (default) → **503** JSON; when on → full anonymous read/write over every REST route (tools generated from the OpenAPI spec) | `smart_vent/backend/mcp_http.py`, `mcp_openapi.py` |
| `mcp_enabled` is a `system_settings` DB key, default `"0"`, toggled via `POST /api/system/mcp`, UI toggle in the settings cog (`frontend/src/App.tsx`), no restart needed | `backend/scheduler.py` `get/set_mcp_enabled`, `routes.py` |
| MCP tool specs carry **no scope/annotation metadata** — destructive tools (restart, restore) are indistinguishable from reads in `ToolSpec` | `mcp_openapi.py` `ToolSpec` dataclass fields |
| `config.yaml` already declares `ingress: true`, `ingress_port: 8099`, `hassio_api: true`, **`auth_api: true`** — the manifest already requests access to the Supervisor auth-validation API, though no code uses it | `smart_vent/config.yaml` |
| Both host ports default to **unpublished** (`8099/tcp: null`, `9099/tcp: null`) — direct exposure is an explicit user opt-in. Precedent: #385 default-mapped 9099 and was reverted in #387 (`c65d35d`); secure-by-default is settled policy (see `plenum-failure-archaeology`) | `config.yaml` `ports:` |
| The unauthenticated state is documented and acknowledged | `docs/mcp.md` ("The MCP endpoint is unauthenticated… Authentication (OAuth for MCP, plus web-UI auth) is tracked separately") |
| Temperature helpers live in `smart_vent/backend/units.py` (`to_f`, `delta_to_f`, `from_f`, `from_f_delta`), imported into `routes.py` under the `_to_f` aliases — CLAUDE.md's "helpers in routes.py" is known drift; trust the repo | `backend/units.py` |
| Runtime deps already include `mcp>=1.28.1`, `uvicorn`, Starlette (transitively) | `smart_vent/pyproject.toml` |

**Consequence**: today, anyone who can reach a published 8099 can download the
entire database, replace it, and restart the add-on, with zero credentials.
Anyone who can reach a published 9099 with `mcp_enabled=1` can do the same via
MCP tools. The only mitigations are "ports unpublished by default" and the
`mcp_enabled` toggle.

---

## 2. UNVERIFIED facts → Phase-0 experiments

None of the following could be verified from this repo. Each is an explicit
Phase-0 experiment. **Do not design on top of any of these until measured.**

- **U1 — Which headers HA ingress adds.** Candidates from HA ecosystem
  knowledge (UNVERIFIED): `X-Ingress-Path`, `X-Remote-User-Id`,
  `X-Remote-User-Name`, `X-Remote-User-Display-Name`, `X-Hass-Source`,
  `X-Forwarded-For`. Whether an admin flag (`X-Hass-Is-Admin` or similar)
  exists at all is UNVERIFIED. → Experiment E2.
- **U2 — Source address of ingress connections.** HA add-on docs have
  historically said ingress requests originate from `172.30.32.2` (the
  Supervisor's ingress proxy on the internal `hassio` network). UNVERIFIED
  here, and version-dependent. → Experiment E2.
- **U3 — Source address of direct-port connections.** Docker host-port
  publishing may NAT the client source to the docker gateway IP. If that
  gateway ever equals/overlaps the ingress proxy address, source-IP trust is
  dead on arrival. → Experiment E2.
- **U4 — Supervisor auth API.** With `auth_api: true`, an add-on is expected
  to be able to validate an HA username/password via
  `POST http://supervisor/auth` with the supervisor token (mechanism used by
  e.g. the Mosquitto add-on). Exact request/response shape and availability:
  UNVERIFIED. → Experiment E3.
- **U5 — Ingress reaches unpublished container ports.** Design option A2
  (below) assumes the Supervisor's ingress proxy can connect to
  `ingress_port` even when that port has no `ports:` host mapping (ingress is
  documented as independent of `ports:`, and Plenum ships today with
  `8099/tcp: null` yet ingress works — strong evidence, but confirm the same
  holds after changing `ingress_port` to a second, never-published listener).
  → Experiment E4.
- **U6 — Standalone-Docker mode has NO supervisor.** `run.sh` explicitly
  handles the no-`SUPERVISOR_TOKEN` case, so any design leaning on Supervisor
  APIs (U4) or the hassio network (U2) MUST have a documented fallback for
  standalone Docker. This is a design constraint, verified in `run.sh`; the
  open question is only what the fallback is.

---

## 3. Phase 0 — recon (no code changes to main; scratch branch allowed)

**Entry gate**: access to a live HA install running the Plenum add-on (or the
`docker-compose.test.yml` stack — note the compose stack has a real HA but you
must publish ports yourself; see `plenum-run-and-operate`).

### E1 — baseline curl matrix (expected codes derived from code as of v0.22.1)

Run from a machine that can reach the published ports. `H=<host>`.

```bash
# Web UI / API — all expected 200 today (that's the problem)
curl -s -o /dev/null -w '%{http_code}\n' http://$H:8099/                       # 200 (SPA)
curl -s -o /dev/null -w '%{http_code}\n' http://$H:8099/api/system/status      # 200
curl -s -o /dev/null -w '%{http_code}\n' http://$H:8099/api/backup             # 200 + full app.db attachment
curl -s -o /dev/null -w '%{http_code}\n' http://$H:8099/api/docs/              # 200 (Swagger UI)
# Destructive — only run on a disposable stack:
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://$H:8099/api/restart    # 200, then the app restarts

# MCP port
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://$H:9099/mcp            # 307 (redirect to /mcp/)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://$H:9099/mcp/           # 503 if mcp_enabled off (default)
curl -s -o /dev/null -w '%{http_code}\n' http://$H:9099/definitely-not-mcp     # 404 (Starlette)
# With mcp_enabled ON (settings cog or POST /api/system/mcp {"mcp_enabled": true}):
curl -s -X POST http://$H:9099/mcp/ \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
# → 200 with an initialize result (stateless streamable-HTTP, json_response=True)

# Spoofed-ingress-header probe — expected: IDENTICAL responses to the above
# (verified: no backend code reads these headers today). Re-run this exact
# probe after Phase 2 lands; it must then