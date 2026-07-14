---
name: plenum-auth-campaign
description: >-
  Executable, decision-gated campaign plan for Plenum issue #373 — adding
  authentication to the web UI (port 8099) and the MCP server (port 9099),
  which are BOTH unauthenticated today and rely entirely on Home Assistant
  ingress in front. Load when working on #373, when asked to design or
  implement auth/login/OAuth/bearer tokens/sessions for Plenum, when reasoning
  about ingress-vs-direct-port trust, MCP OAuth 2.1 authorization, or when
  someone proposes gating access by port number or trusting X-Ingress headers.
  This is a CANDIDATE plan, not decreed scope — the owner has not chosen an
  approach. NOT for general change-gating (load plenum-change-control) or the
  shipped MCP transport internals (plenum-architecture-contract).
---

# Plenum Auth Campaign (issue #373)

> ## ⚡ CURRENT STATUS — handoff (2026-07-13, branch `feature/auth-373`)
>
> The owner has **locked the design** (decision gate cleared) and **Phase 1 has
> shipped on the branch**. A cloud/next session picks up at **Phase 2**. Full
> detail in the "Locked design" and "Numbered implementation phases" sections
> below — the short version:
>
> **Locked decisions (supersede every OPEN DECISION tag below):**
> - **UI = Solution A** (trust-boundary split). Ingress is *always* trusted and
>   **must never be broken**; we do NOT care about the ingress user's role (any
>   HA user via ingress = trusted). Only **direct-port** + **MCP** get the new
>   auth.
> - **Ingress detection = unspoofable transport check** — `auth.is_ingress_request`:
>   TCP peer IP == Supervisor **AND** `X-Remote-User-Id` header present. Peer IP
>   is the load-bearing signal (the header alone is forgeable by any sibling
>   add-on on the flat, unisolated hassio network). Verified from HA Core +
>   Supervisor source and the "App security" dev doc — see updated Phase-0.3/0.4
>   below. **NOT yet verified against a live Supervisor** (no HA in dev).
> - **Direct-port credential (A.1) = HA `/auth` backend** (`auth_api: true`,
>   `POST http://supervisor/auth`). No new password store.
> - **Session (A.2) = HttpOnly + Secure + SameSite=Strict cookie** for the
>   browser; bearer tokens only for programmatic/MCP.
> - **MCP (Solution C) = minted opaque bearer tokens** in the Settings UI,
>   hashed at rest, scoped `read`/`write`/`destructive`, enforced at BOTH 9099
>   and the loopback REST layer; still behind the `mcp_enabled` 503 gate.
> - **Rollout = `require_auth` config option, default TRUE.** Because ingress is
>   always trusted, "default on" does NOT break HA-ingress users — it only forces
>   auth on external/direct ports + MCP. Setting it false restores legacy open
>   behavior (escape hatch).
>
> **Progress against the numbered phases below:**
> - **Phase 1 (Decision gate): CLEARED** — owner's locked choices above.
> - **Phase 2 (Auth middleware skeleton): PARTIAL** — the trust classifier and
>   CSRF hardening landed, but the middleware is **not yet wired** into
>   `middlewares=[...]`, so nothing returns 401 yet (running instance/MCP
>   unaffected; no lock-out before the login path exists). Remaining Phase-2
>   work: wire the middleware behind `require_auth`, and unit-test the flag-off
>   pass-through.
> - **Phases 3–5: NOT STARTED.**
>
> **Commits on `feature/auth-373`:**
> - `f495a12` — CSRF middleware baseline (pre-existing WIP, preserved).
> - `3f9e8f8` — `backend/auth.py` (`is_ingress_request` + `resolve_supervisor_ip`)
>   with the spoof-rejection test matrix (`backend/tests/test_auth.py`, 100% cov);
>   CSRF hardening: the forgeable static `X-Plenum-Source: mcp` loopback marker
>   replaced by a per-process random token (`app["internal_token"]`, constant-time
>   compared, threaded to the MCP dispatcher); "CSRF-only, never auth" boundary
>   comment. Full backend suite green (1137 passed, 94.27%).
>
> **Dev-environment constraint (important for the next session):** local dev is
> NOT attached to Home Assistant — there is no Supervisor, so **ingress paths
> cannot be exercised here at all**. `resolve_supervisor_ip()` returns None
> locally and every request is treated as direct. You can run the app in Docker
> with env vars for direct-port/MCP testing, but ingress-trust + the `/auth`
> login flow can only be truly verified on a real HAOS/supervised install (or in
> CI with a Supervisor stub). Do not claim ingress behavior is verified from a
> local run.
>
> **Next steps (skill numbering):** finish **Phase 2** (wire the middleware
> behind `require_auth`, flag-off pass-through test) → **Phase 3** (direct-port
> login: HA `/auth` → HttpOnly cookie; `require_auth` config + run.sh + UI
> parity) → **Phase 4** (MCP minted tokens) → **Phase 5** (validation, docs,
> goldens). Carryover functional holes to fix along the way: reverse-proxy Host
> mismatch (CSRF Origin vs `request.host`; affects the owner's own
> `plenum.bhavsar.dev`) and live-confirm ingress WS carries an exempt header.

**This is a candidate campaign plan, not approved scope.** Issue #373 is an open
design problem with the owner's own list of unresolved questions. Every design
choice below that the owner has not settled is tagged **OPEN DECISION** with a
recommendation. Do not implement past a decision gate without owner sign-off, and
route all code changes through **plenum-change-control** (do not restate its
gates here). **NOTE (2026-07-13):** the decision gate is now CLEARED — the status
box above records the owner's locked choices, which override the OPEN DECISION
tags throughout this document.

A Sonnet-class session should be able to advance this campaign phase by phase
without supervision by (a) running the Phase-0 recon to establish today's
behavior as ground truth, then (b) walking numbered phases, each with an entry
gate, exact steps, and EXPECTED observations with branch-outs.

## When NOT to use this skill

- General "how do I gate a change / what CI must pass" → **plenum-change-control**.
- Shipped MCP transport internals / loopback design rationale → **plenum-architecture-contract** (owns the MCP loopback invariant) and **plenum-failure-archaeology** (the default-port-mapping revert #387).
- Where a config knob lives / add-a-knob parity → **plenum-config-and-flags**.
- How to write tests CI accepts / coverage gates → **plenum-validation-and-qa**.
- Running the stack to execute the curl matrix → **plenum-run-and-operate** / **plenum-build-and-env**.

## The problem in one paragraph

Both the web UI (`0.0.0.0:8099`) and the MCP server (`0.0.0.0:9099`) are
**completely unauthenticated**. Access control today is entirely "HA ingress sits
in front." If a user maps the raw host port (`config.yaml` `ports:` — both
default to `null` = unpublished), *anyone who can reach that port has full
read/write control*, including destructive endpoints (`/api/restart`,
`/api/restore`, `/api/backup` which downloads the whole SQLite DB). `docs/mcp.md`
already warns "The MCP endpoint is unauthenticated." #373 wants: ingress → HA
account auto-mapped to admin, bypass login; direct port → require auth (local
account or OAuth); MCP → OAuth 2.1 / bearer validation.

## Verified ground truth (as of 2026-07, v0.22.1) — read before designing

Everything here was verified against the repo. Line numbers drift — re-verify with
the commands in Provenance.

| Fact | Where | Note |
|---|---|---|
| ~~No auth middleware exists~~ **STALE as of `feature/auth-373`.** Middlewares are now `[security_headers_middleware, csrf_middleware]`; `backend/auth.py` holds the ingress classifier but is **not yet in the middleware chain**. | `smart_vent/backend/main.py` | The auth middleware still needs wiring (Phase 2). Insert it between `security_headers` and `csrf` so 401s still get security headers and auth rejects before CSRF logic runs. |
| Web UI binds **`0.0.0.0:8099`** | `main.py` `web.TCPSite(runner, "0.0.0.0", PORT)`; `PORT=8099` | Both ingress traffic AND direct-port traffic land on this same listener. |
| MCP binds **`0.0.0.0:9099`** | `main.py` `_start_mcp_server` → `uvicorn.Config(host="0.0.0.0", port=MCP_PORT)`; `MCP_PORT=9099` | Separate ASGI (Starlette + uvicorn) process-in-process. |
| MCP dispatches back to REST over **loopback** `http://127.0.0.1:8099` | `main.py` `base_url`; `mcp_http.dispatch_tool` | So any MCP auth added at the ASGI layer is bypassed if 8099 is directly reachable. Auth must live at the REST layer too, not only at 9099. |
| MCP `/mcp` is gated by `mcp_enabled` toggle → returns **503** when off | `mcp_http.build_asgi_app` `handle_mcp`; `scheduler.get_mcp_enabled` | Default off (`system_settings` key `mcp_enabled` default `"0"`). |
| Both ports default **`null`** (unpublished) in the add-on manifest | `config.yaml` `ports: {8099/tcp: null, 9099/tcp: null}` | The default-mapped-port regression was reverted in `c65d35d` (#387). Do NOT re-map by default. |
| Add-on already declares **`auth_api: true`**, `hassio_api: true`, `homeassistant_api: true` | `config.yaml` | `auth_api: true` means the add-on may call the Supervisor `/auth` endpoint to validate HA usernames/passwords — enables a "log in with your HA account" flow without a new identity store. This is a big lever for #373. |
| `SUPERVISOR_TOKEN` is injected into the container | `run.sh` (logs presence/absence) | The add-on holds a supervisor token; it can call `http://supervisor/...`. |
| Destructive endpoints exist and are unprotected | `routes.py`: `POST /api/restart`, `POST /api/restore`, `GET /api/backup` | `/api/backup` streams the **entire app.db** to the caller — any secret stored in the DB is exfiltrated by one unauthenticated GET. |
| MCP exposes **100% of the REST surface** as tools | `mcp_openapi.build_tool_specs` (one tool per documented `/api/` op) | So "MCP scopes" == "REST endpoint scopes." There is no separate MCP-only capability set. |
| Temp helpers live in `backend/units.py` | `to_f`/`delta_to_f`/`from_f` (imported as `_to_f` etc in `routes.py`) | Older CLAUDE.md copies said `routes.py`; corrected 2026-07-05. Irrelevant to auth but noted so you trust the repo. |
| Settings-cog UI already toggles MCP | `frontend/src/App.tsx` (`McpContext`, `toggleMcp`), `api.ts` `setMcpEnabled` → `POST /api/system/mcp` | Any new auth toggle follows this exact pattern (100%-UI rule). |

**Formerly-UNVERIFIED items — now RESOLVED from HA Core + Supervisor source and
the "App security" dev doc (2026-07-13). Still not exercised against a live
Supervisor (no HA in dev) — confirm with one ingress hit vs one direct hit
before trusting in production, but the mechanism is settled:**

- **Ingress reaches the add-on from the Supervisor container**, which holds a
  fixed address on the flat hassio Docker bridge (`supervisor/docker/network.py`
  allocates the Supervisor at network index [2]; resolvable via the `supervisor`
  hostname). The bridge has **no add-on isolation** — every add-on can open a
  socket to every other add-on's port. So the peer IP is the one signal a
  sibling container cannot forge; a source-based ingress detector IS viable.
- **Headers the Supervisor sets on a validated ingress request** (from
  `supervisor/api/ingress.py` `_init_header`, names in `supervisor/const.py`):
  `X-Remote-User-Id`, `X-Remote-User-Name`, `X-Remote-User-Display-Name`. It sets
  them **only after** validating the ingress session cookie (`ingress_session`),
  and it **strips any client-supplied copies** of those three before re-deriving
  them — but only on the ingress path. On the **direct** port the client controls
  all headers. HA Core additionally sets `X-Ingress-Path` + `X-Hass-Source:
  core.ingress` (undocumented; the `X-Remote-User-Id` triplet is the documented
  add-on-facing contract, so key on it). **`is_admin`/`is_owner` are NOT
  forwarded** — ingress headers say *which* user, never their role. (Moot here:
  owner decided any ingress user is trusted.)
- **The trust rule (implemented in `backend/auth.py`):** trusted-ingress ==
  peer IP == Supervisor **AND** `X-Remote-User-Id` present. Peer IP proves the
  hop is really the Supervisor; the header proves this request took the ingress
  path (not a watchdog probe). **Header presence alone is NEVER sufficient** —
  that is the #373 spoofing trap, forgeable by any sibling add-on.
- **Supervisor `/auth` (enabled by `auth_api: true`)** is HA's documented add-on
  auth backend: `POST http://supervisor/auth` with the HA username+password
  validates against the HA user store (dev doc "App security" → "Use Home
  Assistant user backend"). Confirmed as the direct-port login mechanism (A.1);
  exact success/failure codes still want a live check.

The remaining genuinely-unverifiable-in-dev items: the exact resolved Supervisor
IP, and whether the **ingress WebSocket** upgrade carries `X-Ingress-Path` (HA
Core proxies WS on a separate code path). Both need a real supervised install.

---

## Phase 0 — Recon: establish TODAY's behavior as ground truth

**Entry gate:** none. Always run this first. Do not trust the table above blindly —
reproduce it. Do not write a line of auth code until Phase 0 is green.

### 0.1 Confirm no auth exists (code)

```bash
cd /home/user/smart-thermostat-with-vents/smart_vent
grep -n "middlewares=" backend/main.py                       # expect only security_headers_middleware
grep -rni "authorization\|bearer\|login\|session\|cookie\|@requires_auth" backend/api/routes.py
```
EXPECTED: the second grep returns **nothing auth-related** (only HA WS `auth`
handshake matches live in `ha_client.py`, not routes). If you find an existing
auth decorator → someone started this work; stop and reconcile with that branch.

### 0.2 curl matrix against a running stack

Bring up the container stack (see plenum-run-and-operate / plenum-build-and-env for
how; the compose file is `docker-compose.test.yml`, image var `PLENUM_IMAGE`). Then
run the matrix. `$H = http://localhost:8099`, `$M = http://localhost:9099`.

| # | Command | EXPECTED **today** | If different → branch |
|---|---|---|---|
| 1 | `curl -s -o /dev/null -w '%{http_code}' $H/api/system/status` | `200` (open) | If `401/403` → auth already added; stop, reconcile. |
| 2 | `curl -s -o /dev/null -w '%{http_code}' $H/api/backup` | `200` (streams DB!) | Proves the exfil risk is live. |
| 3 | `curl -s -o /dev/null -w '%{http_code}' -X POST $H/api/restart` | `200`-ish (restarts app) | Run last / against a throwaway stack — it restarts. |
| 4 | `curl -s -o /dev/null -w '%{http_code}' -H 'X-Ingress-Path: /foo' -H 'X-Remote-User-Id: admin' $H/api/system/status` | `200`, **same as #1** | Confirms client-supplied ingress headers are neither required nor validated — they're ignored today, and would be *trusted* only if a naive impl reads them. This is the spoofing baseline. |
| 5 | MCP **off** (default): `curl -s -o /dev/null -w '%{http_code}' -X POST $M/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'` | `503` (from `handle_mcp` gate) | If `200` → MCP is enabled in this stack; toggle it off via `POST $H/api/system/mcp {"mcp_enabled":false}` and retry. |
| 6 | MCP **on** (toggle via UI or `curl -X POST $H/api/system/mcp -d '{"mcp_enabled":true}'`), repeat #5 | `200` + tool list, **no credential required** | Proves MCP is fully open when enabled. |
| 7 | MCP on, call a write tool (e.g. the generated tool for `POST /api/system/enabled`) | succeeds unauthenticated | Confirms destructive MCP access is open. |

Record the actual codes. This matrix is later reused as the **success criterion**:
after auth ships, every cell's expected code changes according to the chosen design
(see "Validation: the auth test matrix").

### 0.3 Ingress trust experiment (the crux of #373)

The whole design hinges on: *can the app tell an ingress request from a direct-port
request in a way an attacker on the direct port cannot forge?* You cannot answer
this from headers alone (the direct-port client controls its own headers). Verify
the **transport-level** distinguisher:

```bash
# In a real HAOS / supervised environment (NOT the plain compose stack, which has
# no supervisor/ingress). Inside the addon container, watch inbound connections:
#   - Hit the UI THROUGH HA ingress (browser via HA sidebar), note source IP.
#   - Hit the raw published port directly, note source IP.
# Compare: ingress traffic arrives from the supervisor's hassio-network address
# (typically 172.30.32.x); direct traffic arrives from the docker gateway / host.
```
EXPECTED: ingress connections originate from the internal supervisor address;
direct connections do not. **If and only if** this holds is a source-based ingress
detector viable. If they are indistinguishable (e.g. both appear as the gateway
IP) → **ingress auto-admin is infeasible; every request must present a credential**
(branch to Solution C / token-only). Mark the result and carry it forward — it
gates Solution A entirely.

Also confirm which headers ingress actually sets, from inside the addon, by logging
`dict(request.headers)` on one endpoint during an ingress hit vs a direct hit.
EXPECTED: ingress hit carries `X-Ingress-Path` (and possibly HA user headers);
direct hit carries whatever the client chose. **The header is only trustworthy if
the connection is already proven to be ingress by transport** — never the reverse.

### 0.4 Verify the Supervisor auth capability

`auth_api: true` is declared. Confirm what it buys you:

```bash
# Inside the addon container (has SUPERVISOR_TOKEN):
curl -s -X POST http://supervisor/auth \
  -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"username":"<ha-user>","password":"<pw>"}' -o /dev/null -w '%{http_code}'
```
EXPECTED (UNVERIFIED): `200` on valid creds, `401` otherwise — this is HA's
documented add-on auth backend. If it works, "log in with your HA account" needs
**no new password store**. If it does not exist/behave as expected in the running
Supervisor version → local-account store becomes necessary (OPEN DECISION below).

**Phase 0 exit gate:** you can state, with evidence, (1) today's curl-matrix codes,
(2) whether ingress is transport-distinguishable from direct, (3) whether the
Supervisor `/auth` endpoint validates credentials. Only then proceed.

---

## Solution menu (ranked) — pick ONE at the Phase-1 gate

Each option is a full design path with mandatory deliverables. **OPEN DECISION:
the owner has not chosen.** Recommendation: **A + C together** (trust-split for the
UI, self-contained bearer for MCP), because it preserves the ingress-bypass UX #373
explicitly wants and satisfies the MCP authorization spec, without adding an
external IdP dependency.

### Solution A — Trust-boundary split (RECOMMENDED for the web UI)

Ingress requests (proven unspoofable per Phase 0.3) are auto-authenticated as
admin and bypass login. Direct-port requests require a credential (local account
**or** OAuth — see sub-decision). One aiohttp auth middleware in front of
`/api/*` classifies the request:

1. Is this connection ingress? (transport-level check from Phase 0.3 — source
   address on the hassio network, **not** a client header). If yes → admin,
   proceed.
2. Else require a valid session cookie or bearer token; 401 otherwise.

**Obligations:**
- **Threat-model deliverable** (write it into `docs/` as a feature page per the
  100%-UI/docs discipline): enumerate every surface × credential state.
- **Spoofing analysis (hard requirement):** you MUST prove the ingress
  distinguisher cannot be forged from the direct port. State exactly what the
  middleware keys on and why a direct-port client cannot reproduce it. If it keys
  on any client-supplied header → **rejected, this is the #373 spoofing trap.**
- Backwards-compat opt-in flag (see OPEN DECISION below).

**OPEN DECISION A.1 — direct-port credential type.** Recommendation: **local
account validated via the Supervisor `/auth` endpoint** (reuse HA identities, no
new password DB) if Phase 0.4 confirms it works; fall back to a local account
store only if it does not.

**OPEN DECISION A.2 — session model.** Recommendation: **HttpOnly, Secure,
SameSite=Strict session cookie** for the browser UI (not a bearer in JS — avoids
XSS token theft; CSP is already locked down in `main.py`). Bearer tokens are for
programmatic/MCP clients (Solution C).

### Solution B — HA-delegated OAuth (Indie Auth / HA as provider)

Direct-port users complete an OAuth flow against Home Assistant itself; the add-on
validates the resulting token. Heavier; introduces redirect-URI handling behind
ingress path rewriting (fragile — `X-Ingress-Path` mangling). **Obligations:** all
of A's, plus a documented redirect-URI story that survives ingress path prefixes.
Recommendation: only if the owner wants true browser SSO and A.1's Supervisor-auth
path proves unavailable.

### Solution C — Self-issued OAuth 2.1 Authorization Server for MCP (RECOMMENDED for MCP)

Per the MCP authorization spec, the MCP server validates a **bearer token** on
every `/mcp` request (in addition to the existing `mcp_enabled` 503 gate). Plenum
either (i) acts as a minimal OAuth 2.1 AS issuing tokens, or (ii) validates
opaque tokens minted in the UI ("Generate MCP token"). Recommendation for v1:
**(ii) — long-lived opaque bearer tokens minted in the settings UI**, hashed at
rest; it satisfies "bearer token validation" without a full AS, and can grow into
a real AS later.

**Obligations:**
- Enforce the token at **both** the ASGI layer (9099) **and** at the REST loopback
  layer — because MCP dispatches to `127.0.0.1:8099` (see ground-truth table). If
  you only guard 9099, anyone on 8099 bypasses MCP auth entirely.
- **Scopes** (OPEN DECISION C.1): map to `read` / `write` / `destructive`. Since
  MCP tools == REST endpoints, classify each endpoint. Minimum: `destructive`
  covers `POST /api/restart`, `POST /api/restore`, `GET /api/backup`. Recommend a
  per-token scope grant, default `read`.
- **Interaction with the `mcp_enabled` toggle:** auth is *in addition to* the
  toggle, never instead of it. Off → 503 regardless of token.
- **Token storage vs /api/backup (hard requirement):** `GET /api/backup` streams
  the entire `app.db`. If tokens (or their hashes) live in the DB, a backup
  download exfiltrates them. Either (a) store only a slow salted hash (argon2/
  bcrypt) so a leaked backup isn't trivially usable, AND put `/api/backup` behind
  `destructive` scope; or (b) store tokens outside the DB. **Do NOT store raw
  secrets in SQLite.** This is called out explicitly in #373.

---

## Known WRONG paths — fenced off (do not "rediscover" these)

| Anti-pattern | Why it's wrong |
|---|---|
| **Gate by port number alone** (e.g. "8099 = trusted, others = untrusted") | Both the UI and MCP bind `0.0.0.0`, and ingress + direct traffic share port 8099. Port tells you nothing about trust. |
| **Trust client-supplied `X-Ingress-Path` / `X-Remote-User-Id` on the direct port** | The direct-port client sets its own headers. Phase 0.2 #4 shows they're free-form. Ingress detection MUST be transport-level (Phase 0.3) and unspoofable. |
| **Auth only in the SPA** (React route guards / hide the login) | The API is the trust boundary. curl bypasses the SPA entirely (Phase 0.2). Auth lives in the aiohttp middleware. |
| **Guard only the 9099 ASGI layer for MCP** | MCP dispatches to REST over loopback `127.0.0.1:8099`; a direct 8099 hit bypasses it. Enforce at the REST layer. |
| **Break existing open-port users silently** | Backwards-compat is an explicit open question in #373. Flipping on mandatory auth bricks anyone relying on the raw port. Ship behind an **opt-in/migration flag** (OPEN DECISION below), default preserving current behavior until the owner decides the default flips. |
| **Store raw tokens/passwords in `app.db`** | `/api/backup` downloads the DB. Hash at rest + scope-protect backup, or store outside the DB. |
| **Re-map ports to non-null by default** to "make auth reachable" | Reverted in `c65d35d`/#387 (see plenum-failure-archaeology). Ports stay `null` by default. |

**OPEN DECISION — backwards compatibility default.** Recommendation: introduce a
`require_auth` system flag (config.yaml option + `system_settings` key + UI
toggle). Default `false` for one release (behavior unchanged; log a loud
deprecation warning when the raw port is published without auth), flip default to
`true` in a subsequent major with a migration note. The owner decides the flip
timing.

---

## Numbered implementation phases

Each phase: **entry gate → steps → EXPECTED → branch-outs.** Do not skip a gate.

### Phase 1 — Decision gate (no code)

**Entry gate:** Phase 0 exit gate satisfied.
**Steps:** Present Phase-0 evidence + this solution menu to the owner. Get an
explicit choice for: the UI approach (A/B), the MCP approach (C variant), A.1
credential type, A.2 session model, C.1 scopes, and the backwards-compat default.
**EXPECTED:** owner selects. **Branch:** if owner defers → stop; this is a design
issue, not an implementation ticket. Do not proceed on assumption.

### Phase 2 — Auth middleware skeleton (UI, behind flag OFF)

**Entry gate:** Phase 1 decisions recorded; `require_auth` default = off.
**Steps:**
- Add an aiohttp middleware to the `middlewares=[...]` list in `main.py`, ordered
  so it runs *before* handlers but composes with `security_headers_middleware`.
- When `require_auth` is off → pass through (no behavior change — this keeps
  existing users working and keeps the curl matrix green).
- Implement the ingress transport-check helper (per Phase 0.3), unit-tested for
  the spoof case: a request with forged `X-Ingress-*` headers but a non-ingress
  source MUST classify as untrusted.
**EXPECTED:** full existing test suite + curl matrix unchanged (flag off).
**Branch:** any pre-existing test flips → the middleware is leaking behavior when
off; fix before continuing.

### Phase 3 — Credential path (direct port)

**Entry gate:** Phase 2 merged.
**Steps:** implement A.1 (Supervisor `/auth` login → session cookie, or local
store) + login endpoint + login page. Wire the session model (A.2). Add
`require_auth` as a `config.yaml` option **and** `run.sh` `bashio::config` read
(parity enforced by `test_addon_config.py`) **and** a UI toggle + helper text
(100%-UI rule). See plenum-config-and-flags for the add-a-knob checklist.
**EXPECTED:** with flag ON, direct-port unauthenticated requests → 401; ingress
requests → 200 (admin); valid login → 200.
**Branch:** ingress requests get 401 → your transport check is wrong or too strict;
revisit Phase 0.3 evidence.

### Phase 4 — MCP bearer validation (Solution C)

**Entry gate:** Phase 3 merged; C variant chosen.
**Steps:** enforce bearer at the ASGI `handle_mcp` (after the `mcp_enabled` 503
gate) AND at the REST loopback boundary. Mint/hash tokens; scope-check
`read`/`write`/`destructive`. Put `/api/backup`, `/api/restore`, `/api/restart`
behind `destructive`. Add token-management UI (mint/revoke/scope) — 100%-UI rule.
**EXPECTED:** MCP on + no token → 401; on + read token + write tool → 403; on +
write token → 200; off → 503 regardless.
**Branch:** a write tool succeeds with a read token → scope enforcement is at the
wrong layer (must be REST-side, since dispatch is loopback).

### Phase 5 — Validation & promotion

Route all of this through **plenum-change-control** (classification, CI gates,
reviewer evidence) and **plenum-validation-and-qa** (how to add tests CI accepts;
coverage gates — backend 93.9 as of 2026-07, full table in its §6,
re-verify). Do not restate those here.
- Backend: pytest integration tests for every matrix cell in both `require_auth`
  states and both temperature units (Celsius-mode pattern lives in
  plenum-validation-and-qa).
- E2E: a login round-trip; a spoofed-header-on-direct-port test that MUST 401.
- Docs: threat-model + auth feature page in `docs/`; update `docs/mcp.md` (remove
  the "unauthenticated" warning once C lands); README/CHANGELOG per
  plenum-docs-and-writing.
- Visual regression: any new login page / settings toggle regenerates goldens
  (both F and C legs) — see plenum-ci-and-release.

---

## Validation: the auth test matrix (the measurable success criterion)

The campaign is "done" when this matrix passes as automated tests. Fill the
EXPECTED column at Phase 1 from the chosen design; the values below assume
A+C with `require_auth` ON.

| Surface | Credential state | EXPECTED code |
|---|---|---|
| `GET /api/system/status` via ingress | none (auto-admin) | 200 |
| `GET /api/system/status` direct | none | 401 |
| `GET /api/system/status` direct | valid session | 200 |
| `GET /api/system/status` direct | forged `X-Ingress-*` headers | **401** (spoof rejected) |
| `GET /api/backup` direct | non-destructive cred | 403 |
| `GET /api/backup` direct | destructive cred / admin | 200 |
| `POST /api/restart` direct | non-destructive cred | 403 |
| `POST /mcp` | `mcp_enabled` off | 503 (regardless of token) |
| `POST /mcp` tools/list | on, no bearer | 401 |
| `POST /mcp` read tool | on, read token | 200 |
| `POST /mcp` write tool | on, read token | 403 |
| `POST /mcp` write tool | on, write token | 200 |
| `POST /mcp` destructive tool | on, write (not destructive) token | 403 |
| Any surface | `require_auth` OFF (compat mode) | same as today's Phase-0 matrix |

The last row is the backwards-compat guarantee: with the flag off, the Phase-0
baseline codes must be reproduced exactly.

## Provenance and maintenance

Volatile facts, date-stamped `(as of 2026-07, v0.22.1)`; re-verify with:

- No auth middleware today: `grep -n "middlewares=" smart_vent/backend/main.py` (expect only `security_headers_middleware`).
- Bind addresses/ports: `grep -n "TCPSite\|uvicorn.Config\|PORT =\|MCP_PORT =" smart_vent/backend/main.py`.
- MCP loopback base URL: `grep -n "base_url" smart_vent/backend/main.py`.
- MCP 503 gate: `grep -n "503\|is_enabled" smart_vent/backend/mcp_http.py`.
- Ports default null: `grep -n "9099\|8099" smart_vent/config.yaml`; add-on auth caps: `grep -n "auth_api\|hassio_api\|homeassistant_api" smart_vent/config.yaml`.
- Destructive endpoints: `grep -n "api/restart\|api/backup\|api/restore" smart_vent/backend/api/routes.py`.
- MCP toggle UI/API: `grep -n "mcp\|Mcp" smart_vent/frontend/src/App.tsx smart_vent/frontend/src/api.ts`.
- "Unauthenticated" warning still present: `grep -ni "unauthenticated" docs/mcp.md`.
- Coverage gates (drift): backend in `smart_vent/pyproject.toml`; frontend in `smart_vent/frontend/vite.config.ts`.
- **UNVERIFIED items** (Supervisor `/auth` behavior, ingress source IP, exact ingress headers): confirmed only by running Phase 0.3 / 0.4 in a real supervised HA environment — this repo's compose stack has no supervisor, so they cannot be verified here.
- The `require_auth` flag, scopes, and every OPEN DECISION are **proposals**, not shipped. As of v0.22.1, #373 is unstarted: `grep -rni "require_auth" smart_vent/` returns nothing.
