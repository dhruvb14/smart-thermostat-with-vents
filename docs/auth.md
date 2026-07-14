# Authentication & the trust model

Plenum listens on two ports inside the add-on: the web UI / REST API (`8099`)
and the MCP server (`9099`). Both bind `0.0.0.0`. This page describes who is
trusted, how, and why — the threat model behind the authentication added in
issue #373.

## The one thing that never changes: Home Assistant ingress

When you open Plenum from the Home Assistant sidebar, the request is
reverse-proxied by the **Supervisor**, which has *already* authenticated you
against your Home Assistant account. Those requests are **always trusted** and
never see a login screen — that is the whole point of ingress, and it is
preserved no matter how the settings below are configured.

## `require_auth` — the master switch

A single add-on option, **`require_auth`** (default **`true`**), decides whether
the *non-ingress* surfaces are gated:

| `require_auth` | HA ingress | Direct port (8099) | MCP (9099) |
|---|---|---|---|
| `true` (default) | trusted, no login | **login required** (HA account → session cookie) | **bearer token required** |
| `false` (legacy) | trusted, no login | open (pre-#373 behavior) | open |

Because ingress is always trusted, turning the switch **on by default does not
break anyone using Plenum through Home Assistant** — it only matters if you have
*published* the raw `8099`/`9099` host ports (both are unpublished by default).
`require_auth` is a deployment setting (add-on **Configuration** tab); it is
surfaced read-only in the UI and is not a runtime toggle, so an unauthenticated
visitor cannot turn it off.

## How ingress is told apart from a direct-port caller (the crux)

The design hinges on distinguishing an ingress request from a direct-port
request **in a way a direct-port attacker cannot forge**.

The Supervisor stamps `X-Remote-User-Id` (and `-Name` / `-Display-Name`) on a
validated ingress request. But those are **plain HTTP headers** — and every
add-on sits on one flat, unisolated Docker bridge network, so a sibling add-on
(or anything that can open a socket to the port) can send
`X-Remote-User-Id: whoever` itself. **Header presence alone is never trusted.**

The unforgeable signal is the **TCP peer address**. Ingress traffic always
arrives from the Supervisor's container, which holds a fixed address on the
hassio network. So the rule (`backend/auth.py::is_ingress_request`) is:

> **trusted-ingress == the TCP peer IP is the Supervisor *and* `X-Remote-User-Id`
> is present.**

The peer IP proves the hop is really the Supervisor; the header proves this
particular request took the ingress code path (not, say, a watchdog probe).
Neither alone is sufficient. A direct-port caller can spoof the header but not
the source address of a real TCP handshake. If no Supervisor is resolvable at
all (local dev, plain Docker, CI), **nothing** is classified as ingress and
every request must authenticate.

## Direct-port login (the web UI)

When `require_auth` is on and a request is not ingress, `/api/*` and the `/ws`
stream require a credential. The SPA reads the public `/api/auth/status` probe
and, if unauthenticated, renders a login screen.

- **Credential:** your **Home Assistant username + password**, validated by the
  add-on against the Supervisor's `/auth` backend (`auth_api: true` →
  `POST http://supervisor/auth`). Plenum stores no password of its own. This
  backend validates the **first factor only** — it does not run Home Assistant's
  MFA/TOTP step, so direct-port login is not MFA-enforced (see known
  limitations). It also requires a Supervisor, so it is unavailable in
  standalone/plain-Docker deployments.
- **Session:** on success the backend sets a **stateless, HMAC-signed session
  cookie** — `HttpOnly` (invisible to JavaScript, so XSS can't steal it),
  `SameSite=Strict` (not sent cross-site), and `Secure` **whenever the request
  is over TLS**. The signing secret is a per-install random value persisted to a
  file **outside `app.db`** (`<data-dir>/.session_secret`, mode `0600`), so a
  downloaded database backup cannot be used to forge sessions.

The SPA shell and static assets are always served (they carry no data); the
trust boundary is the API underneath, plus `/api/healthz` stays open for the
container health probe.

## MCP bearer tokens

When `require_auth` is on, every `/mcp` request must carry
`Authorization: Bearer <token>` — in addition to the existing `mcp_enabled`
toggle (the endpoint still returns `503` when MCP is switched off, regardless of
token). Tokens are minted in the UI (Settings page → **MCP access tokens**).

- **Storage:** only a **SHA-256 hash** of the token is stored. The raw secret is
  256 bits of entropy shown **once** at mint time and never again — the hash is
  not brute-forceable, so a leaked backup cannot be replayed.
- **Scopes:** `read` < `write` < `destructive`. A token may perform any
  operation at or below its scope. `GET` is `read`; other verbs are `write`;
  `/api/restart`, `/api/restore`, `/api/backup` (it streams the whole DB), and
  token management itself are `destructive`.
- **Dual-layer enforcement.** The 9099 ASGI layer authenticates the bearer
  (invalid/missing → `401`). Because MCP dispatches each tool back through the
  REST API over loopback, **scope is enforced at the REST boundary** — the
  dispatcher threads the granted scope, and the auth middleware rejects a call
  whose required scope exceeds it (`403`). Guarding only the 9099 layer would be
  bypassable; the REST-side check is the one that counts.

## Threat model matrix

Assumes `require_auth = true` (the default). The last row is the
backwards-compatibility guarantee.

| Surface | Credential | Result |
|---|---|---|
| `GET /api/system/status` via ingress | none (auto-admin) | `200` |
| `GET /api/system/status` direct | none | `401` |
| `GET /api/system/status` direct | valid session cookie | `200` |
| `GET /api/rooms` direct | forged `X-Remote-User-Id`, no Supervisor peer | `401` (spoof rejected) |
| `POST /api/auth/login` | valid HA creds | `200` + session cookie |
| `POST /api/auth/login` | invalid creds | `401` |
| `POST /api/auth/login` | no Supervisor backend | `503` |
| `POST /mcp` | `mcp_enabled` off | `503` (regardless of token) |
| `POST /mcp` | on, no/invalid bearer | `401` |
| MCP write tool | on, `read` token | `403` |
| MCP write tool | on, `write` token | `200` |
| MCP destructive tool (`backup`/`restart`) | on, `write` token | `403` |
| MCP destructive tool | on, `destructive` token | `200` |
| Any surface | `require_auth = false` | open (pre-#373 behavior) |

## Operational notes & known limitations

- **Run the direct port behind TLS.** The session cookie is marked `Secure` only
  when the connection is TLS. Over plain HTTP it cannot be `Secure`, and you are
  also sending credentials in the clear — put a TLS-terminating reverse proxy in
  front, or just use ingress.
- **Reverse proxies.** The SPA sends an `X-Requested-With` header, so its REST
  requests clear the CSRF check via the exempt-header path and work even behind a
  proxy that rewrites `Host`. Two things still key on what the add-on sees on the
  wire: the `Secure` cookie flag (set only when the add-on's own connection is
  TLS) and the CSRF `Origin` check on the **WebSocket** `/ws` handshake (which
  can't send a custom header). If you front Plenum with a TLS-terminating /
  Host-rewriting proxy, forward the original `Host` and scheme so those two agree
  with the external URL.
- **Not verified against a live Supervisor.** The development/CI environment is
  not attached to Home Assistant, so the ingress-trust path and the Supervisor
  `/auth` login are covered by unit/integration tests and confirmed from HA
  Core / Supervisor source, but have **not** been exercised end-to-end on a real
  supervised install. Confirm with one ingress hit vs one direct hit before
  relying on it in production. The exact resolved Supervisor IP, the `/auth`
  success/failure codes, and whether the ingress **WebSocket** upgrade carries
  the expected headers each need a real supervised environment to verify.
- **Direct-port login is first-factor only (no MFA/2FA).** The web-UI login
  validates your HA username + password against the Supervisor `/auth` backend,
  which does **not** run Home Assistant's MFA/TOTP step — MFA lives in HA's
  interactive `login_flow`, not the add-on auth backend. A user with 2FA enabled
  can therefore sign in to a published port with just their password. **Ingress
  is unaffected:** ingress users authenticate through HA's real login flow, so
  MFA *is* enforced on the ingress path. If you need MFA end-to-end for external
  access, use ingress, or front the direct port with an authenticating reverse
  proxy. An MFA-aware login via HA's `login_flow` is tracked as a follow-up.
- **Standalone / plain-Docker (no Supervisor) can't use `require_auth` login.**
  Direct-port login needs the Supervisor `/auth` backend; with no Supervisor the
  login endpoint returns `503`, and since nothing is classifiable as ingress
  either, the UI becomes unreachable when `require_auth` is on. Run the add-on
  under Home Assistant to use `require_auth`, or in standalone Docker keep
  `require_auth=false` and put an authenticating reverse proxy (e.g. oauth2-proxy
  / Authelia) in front of the port. The long-lived `HA_TOKEN` used for the HA
  data connection **cannot** validate user credentials, so it is not a
  substitute. Supervisor-independent login is tracked as a follow-up.
- **No login rate-limiting yet.** Brute-force protection relies on the Home
  Assistant backend and the fact that the direct port is unpublished by default;
  a per-IP throttle is a candidate future hardening.
