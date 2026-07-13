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

from aiohttp import web

log = logging.getLogger(__name__)

# Header the Supervisor stamps on an authenticated ingress request. Documented
# as the add-on-facing contract, so it is the signal we key on (over the
# internal ``X-Hass-Source: core.ingress`` set by HA Core, which is an
# undocumented implementation detail).
INGRESS_USER_HEADER = "X-Remote-User-Id"

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
    return peername[0]


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
