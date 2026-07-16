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
| `true` (default) | trusted, no login | **login required** (HA account, or OIDC SSO — see below — → session cookie) | **bearer token required** |
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
- **Session:** on success the backend sets a **stateless, signed session
  cookie** — an **HS256 JWT** (minted/verified with `joserfc`, the same JOSE
  library the OIDC login uses; decoding pins the algorithm so a token claiming a
  different `alg` is refused) — marked `HttpOnly` (invisible to JavaScript, so
  XSS can't steal it), `SameSite=Strict` (not sent cross-site), and `Secure`
  **whenever the request is over TLS**. The signing secret is a per-install
  random value persisted to a file **outside `app.db`**
  (`<data-dir>/.session_secret`, mode `0600`), so a downloaded database backup
  cannot be used to forge sessions.

The SPA shell and static assets are always served (they carry no data); the
trust boundary is the API underneath, plus `/api/healthz` stays open for the
container health probe.

## OIDC single sign-on (optional, closes the two direct-port gaps)

The HA-account login above has two limitations: it validates only the first
factor (no MFA — HA's TOTP step runs in its interactive login flow, not the
add-on `/auth` backend), and it needs a Supervisor (so standalone / plain-Docker
installs can't use it). **OpenID Connect single sign-on** closes both: you point
Plenum at your own identity provider (Authelia, Authentik, Keycloak, Google,
Microsoft Entra ID, …), the IdP enforces MFA, and no Supervisor is required.

When OIDC is configured, the direct-port login screen shows a **"Sign in with
&lt;provider&gt;"** button instead of the username/password form, and the
HA-password login route is **disabled server-side** (returns `403`) — so the
weaker first-factor-only path can't be reached even by calling the endpoint
directly. **Ingress is unaffected** (always trusted, never sees this). **MCP is
unaffected** — it keeps its bearer tokens; to add SSO in front of the MCP port,
see `docs/mcp.md`.

**OIDC is configured entirely outside the Plenum UI** — via add-on options (the
Supervisor **Configuration** tab) or container environment variables — so an
unauthenticated visitor can never reach the configuration, and you never have to
disable auth to reach the UI off-HAOS. Each add-on option key uppercases to the
env var of the same name, so standalone Docker sets the env var directly:

| Add-on option / env var | Required | Meaning |
|---|---|---|
| `oidc_configuration_url` / `OIDC_CONFIGURATION_URL` | yes | Your IdP's `…/.well-known/openid-configuration` discovery URL. |
| `oidc_client_id` / `OIDC_CLIENT_ID` | yes | The OAuth client you register with the IdP for Plenum. |
| `oidc_client_secret` / `OIDC_CLIENT_SECRET` | yes | That client's secret (a `password`-typed option, masked in the add-on UI). |
| `plenum_external_url` / `PLENUM_EXTERNAL_URL` | yes | Public base URL of the Plenum web UI. The **redirect URI** is `<plenum_external_url>/api/auth/oidc/callback` — register exactly that at your IdP. |
| `oidc_scopes` / `OIDC_SCOPES` | no | Space-separated scopes (default `openid email profile`; `openid` is added if you omit it). |
| `oidc_allowed_users_glob` / `OIDC_ALLOWED_USERS_GLOB` | no | Glob over the authenticated identity (email, then username, then `sub`) allowed in. Default `*` (**anyone** your IdP authenticates — narrow this in production, e.g. `*@example.com`). |
| `oidc_provider_name` / `OIDC_PROVIDER_NAME` | no | Label for the button, e.g. `Authelia` (default `SSO`). |

OIDC is treated as configured only when the four required values are all set; a
partial configuration logs a warning and leaves the HA-password login in place.

**Standalone Docker example.** This is the same base command as the README's
Option B, with the OIDC variables added instead of `REQUIRE_AUTH=false` —
`require_auth` stays at its default `true`, so authentication is never
disabled:

```bash
docker run -d \
  --name smart-vent \
  -p 8099:8099 \
  -v /path/to/data:/data \
  -e DATA_DIR=/data \
  -e HA_URL=https://your-ha-instance.com \
  -e HA_TOKEN=your_long_lived_token \
  -e TIMEZONE=America/New_York \
  -e OIDC_CONFIGURATION_URL=https://your-idp.example.com/.well-known/openid-configuration \
  -e OIDC_CLIENT_ID=plenum \
  -e OIDC_CLIENT_SECRET=your_client_secret \
  -e PLENUM_EXTERNAL_URL=https://plenum.example.com \
  ghcr.io/dhruvb14/smart-thermostat-with-vents:latest
```

Register `https://plenum.example.com/api/auth/oidc/callback` as the redirect
URI at your IdP — it must exactly match `PLENUM_EXTERNAL_URL` above. Add
`-e OIDC_ALLOWED_USERS_GLOB=...` to narrow who can sign in beyond "anyone your
IdP authenticates".

- **Flow.** Standard OAuth 2.1 **Authorization Code + PKCE**. The IdP validates
  the login (and MFA); Plenum validates the returned ID token's signature and
  claims (`joserfc`), checks the allowlist, then issues the **same** signed
  session cookie as the password path (so the session model is identical).
- **CSRF / state.** The `state`, PKCE `code_verifier`, and `nonce` ride in a
  short-lived, signed (HS256 JWT) `plenum_oidc_state` cookie (`SameSite=Lax` so it
  survives the redirect back from the IdP) — nothing is written to `app.db`, so a
  backup still can't forge a login.
- **Algorithm pinning.** ID tokens must be signed with an asymmetric algorithm
  (`RS*`/`ES*`/`PS*`); `alg: none` and symmetric `HS*` are refused, defeating the
  classic key-confusion attack.
- **Run behind TLS.** As with the password path, the session cookie is `Secure`
  only over TLS, and OAuth redirect URIs should be HTTPS — terminate TLS in front
  and forward the original `Host`/scheme so the redirect URI matches.

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
- **HA-password direct-port login is first-factor only (no MFA/2FA) — configure
  OIDC for MFA.** The HA username/password login validates against the Supervisor
  `/auth` backend, which does **not** run Home Assistant's MFA/TOTP step (that
  lives in HA's interactive login flow). A user with 2FA enabled can therefore
  sign in to a published port with just their password. **Ingress is unaffected**
  (ingress users go through HA's real login flow, so MFA *is* enforced there). To
  enforce MFA on the direct port, configure **OIDC single sign-on** (above) — the
  IdP enforces MFA and the HA-password path is then disabled — or front the port
  with an authenticating reverse proxy.
- **Standalone / plain-Docker (no Supervisor): use OIDC, or keep
  `require_auth=false`.** The HA-password login needs the Supervisor `/auth`
  backend; with no Supervisor it returns `503`, and since nothing is classifiable
  as ingress either, the UI would be unreachable when `require_auth` is on **and**
  OIDC is not configured. Fixes, in order of preference: (1) configure **OIDC
  single sign-on** (above) — it needs no Supervisor and gives a working,
  MFA-capable login; (2) keep `require_auth=false` and put an authenticating
  reverse proxy (oauth2-proxy / Authelia) in front of the port. The long-lived
  `HA_TOKEN` used for the HA data connection **cannot** validate user credentials,
  so it is not a substitute.
- **No login rate-limiting yet.** Brute-force protection relies on the Home
  Assistant backend and the fact that the direct port is unpublished by default;
  a per-IP throttle is a candidate future hardening.
