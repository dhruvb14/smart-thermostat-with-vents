"""Authentication trust boundary for Plenum (Issue #373).

Plenum listens on ``0.0.0.0`` for both the web UI (8099) and the MCP server
(9099). Two classes of caller reach that listener and they get very different
trust:

* **Home Assistant ingress** — the Supervisor reverse-proxies the request after
  it has already validated the user's HA ingress session cookie. These callers
  are fully trusted (any HA user; we do not care about their role — see #373
  discussion) and never see a login screen.
* **Everything else** ("direct port") — a browser hitting a published port, a
  reverse proxy, another add-on on the Supervisor network, or an attacker.
  These must present a credential (session cookie for the UI, bearer token for
  MCP) once ``require_auth`` is on.

The whole design hinges on telling those two apart in a way a direct-port caller
**cannot forge**. This module owns that decision.

Why not trust the ``X-Remote-User-*`` headers alone
----------------------------------------------------
The Supervisor sets ``X-Remote-User-Id`` / ``-Name`` / ``-Display-Name`` on
ingress requests *after* validating the ingress session cookie (documented at
developers.home-assistant.io "App security"). But those are plain HTTP headers.
The Supervisor puts every add-on on one **flat, unisolated** Docker bridge
network (verified in ``supervisor/docker/network.py``), so a sibling add-on — or
anything else that can open a socket to our port — can send
``X-Remote-User-Id: whoever`` itself and bypass the Supervisor entirely. Header
presence is therefore a *necessary* signal but never a *sufficient* one.

The unforgeable part is the **TCP peer address**. Ingress traffic always arrives
from the Supervisor's container, which holds a fixed, predictable address on the
hassio network (``supervisor/docker/network.py`` allocates it at index [2];
resolvable via the ``supervisor`` hostname the add-on can already reach). A
sibling container cannot present the Supervisor's source address on a real TCP
handshake without controlling that address. So the rule is:

    trusted-ingress  ==  peer IP is the Supervisor  AND  X-Remote-User-Id present

Peer IP proves the hop really is the Supervisor; the header then proves this
particular request came through the ingress code path (not some other
Supervisor→add-on traffic such as a watchdog health probe). Neither alone is
enough.

NOTE (unverified against a live Supervisor): the exact resolved Supervisor IP
and header set are confirmed from HA Core/Supervisor source, not from a running
HAOS box (the local/compose stack has no Supervisor). Confirm with one ingress
hit vs one direct hit before relying on this in production. See the
``plenum-auth-campaign`` skill, Phase 0.3.
"""

from __future__ import annotations

import logging
import os
import socket

import aiohttp
from aiohttp import web

log = logging.getLogger(__name__)

# Home Assistant's documented add-on auth backend (enabled by ``auth_api: true``
# in config.yaml). We POST the user's HA credentials here with the add-on's
# Supervisor token; a 200 means "valid HA user." This lets direct-port login
# reuse HA identities with no password store of our own.
SUPERVISOR_AUTH_URL = "http://supervisor/auth"
# How long to wait on the Supervisor before giving up a login attempt.
_AUTH_TIMEOUT_SECONDS = 10

# Header the Supervisor stamps on an authenticated ingress request. Documented
# as the add-on-facing contract, so it is the signal we key on (over the
# internal ``X-Hass-Source: core.ingress`` set by HA Core, which is an
# undocumented implementation detail).
INGRESS_USER_HEADER = "X-Remote-User-Id"

# API paths that must stay reachable without a credential even when
# ``require_auth`` is on:
#   * ``/api/healthz`` — the container/HA watchdog probe (``curl -sf`` in
#     docker-compose); a 401 here marks the add-on unhealthy and takes it down.
#   * the ``/api/auth/*`` endpoints below — the way *in* (login) and *out*
#     (logout), plus a public status probe the SPA reads to decide whether to
#     render the login screen. 401-ing these would make login impossible.
#   * the OIDC login/callback endpoints (#464) — the way *in* via single
#     sign-on. Both must be reachable without a credential (you have none yet),
#     and the callback carries only the IdP's ``code``/``state`` on its query
#     string, which is validated against the signed state cookie.
_PUBLIC_API_PATHS = frozenset(
    {
        "/api/healthz",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/status",
        "/api/auth/oidc/login",
        "/api/auth/oidc/callback",
    }
)

# Hostname the add-on uses to reach the Supervisor; resolves to its fixed
# address on the hassio Docker network. Overridable for tests / unusual setups.
SUPERVISOR_HOST = os.environ.get("SUPERVISOR_HOST", "supervisor")


def resolve_supervisor_ip() -> str | None:
    """Resolve the Supervisor's address on the hassio network, or None.

    Returns None when the name does not resolve — i.e. we are not running under a
    Supervisor (local dev, plain Docker, CI). Callers treat "no Supervisor" as
    "no request can be ingress," so every request must then authenticate.
    """
    try:
        return socket.gethostbyname(SUPERVISOR_HOST)
    except OSError:
        return None


def _peer_ip(request: web.Request) -> str | None:
    """The TCP source address of the connection, or None if unavailable."""
    transport = request.transport
    if transport is None:
        return None
    peername = transport.get_extra_info("peername")
    if not peername:
        return None
    # peername is (host, port) for IPv4 / (host, port, flowinfo, scopeid) for v6.
    return str(peername[0])


def is_ingress_request(request: web.Request) -> bool:
    """True iff this request provably arrived via Home Assistant ingress.

    Two conditions, BOTH required (see module docstring):
      1. the TCP peer is the Supervisor (unforgeable transport signal), and
      2. the Supervisor's ingress user header is present.

    A direct-port caller can spoof (2) but not (1); Supervisor→add-on traffic
    that is not ingress (e.g. a watchdog probe) satisfies (1) but not (2). Only
    genuine ingress satisfies both.

    The resolved Supervisor IP is cached on the app so we resolve once per boot,
    not per request. If there is no Supervisor (local dev / CI), nothing is ever
    classified as ingress.
    """
    supervisor_ip = request.app.get("supervisor_ip")
    if not supervisor_ip:
        return False
    if _peer_ip(request) != supervisor_ip:
        return False
    return INGRESS_USER_HEADER in request.headers


def is_protected_path(path: str) -> bool:
    """True if *path* sits behind the auth boundary.

    The REST API and the ``/ws`` live stream are the real trust boundary — they
    return data and accept mutations. The SPA shell and its static assets are
    just files (no data), so they are always served: a direct-port visitor can
    fetch the bundle and see the login screen, but every data path underneath it
    requires a credential. The handful of public API paths (health + the auth
    endpoints) are the documented exceptions in ``_PUBLIC_API_PATHS``.
    """
    if path == "/ws":
        return True
    if path in _PUBLIC_API_PATHS:
        return False
    return path.startswith("/api/")


def unauthorized() -> web.Response:
    """A uniform 401 for the auth boundary.

    JSON body so the SPA's fetch wrapper surfaces a clean message; deliberately
    generic (a spoof attempt learns nothing about *why* it was rejected).
    """
    return web.json_response({"error": "Authentication required"}, status=401)


class SupervisorUnavailable(Exception):
    """Raised when the Supervisor ``/auth`` backend cannot be used to validate a
    login — i.e. there is no ``SUPERVISOR_TOKEN`` (running outside a supervised
    install: local dev, plain Docker). The caller turns this into a clear 503,
    distinct from a genuine 401 (bad credentials)."""


async def validate_ha_credentials(username: str, password: str) -> bool:
    """Validate an HA username/password against the Supervisor ``/auth`` backend.

    Uses the add-on's ``SUPERVISOR_TOKEN`` (``auth_api: true``) to call
    ``POST http://supervisor/auth`` — Home Assistant's documented add-on auth
    backend — so Plenum never stores or sees a password at rest. Returns True iff
    the Supervisor accepts the credentials (HTTP 200), False otherwise.

    Raises :class:`SupervisorUnavailable` when there is no Supervisor token, so
    the caller can return a 503 ("login backend unreachable") rather than a
    misleading 401.

    The add-on token MUST be sent in the ``X-Supervisor-Token`` header, **not**
    ``Authorization``. The ``/auth`` endpoint reserves the ``Authorization``
    header for the *user's* Basic credentials, so a ``Bearer <supervisor-token>``
    there is parsed as a (malformed) user login and **every** attempt fails with
    a misleading 401 — regardless of the real password or whether MFA is set.
    Send the add-on token via ``X-Supervisor-Token`` and the user credentials in
    the JSON body (developers.home-assistant.io "Supervisor → Endpoints → auth").
    Only HTTP 200 is treated as success.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise SupervisorUnavailable
    timeout = aiohttp.ClientTimeout(total=_AUTH_TIMEOUT_SECONDS)
    async with (
        aiohttp.ClientSession(timeout=timeout) as http,
        http.post(
            SUPERVISOR_AUTH_URL,
            headers={
                "X-Supervisor-Token": token,
                "Content-Type": "application/json",
            },
            json={"username": username, "password": password},
        ) as resp,
    ):
        if resp.status != 200:
            # A non-200 is not necessarily a bad password: 403 (auth_api not
            # granted), 400 (bad request shape), or 5xx (Core/Supervisor down)
            # all land here. Log the status so the cause is diagnosable in the
            # add-on logs; the caller still returns a generic client message.
            log.warning("Supervisor /auth returned HTTP %s", resp.status)
        return resp.status == 200
