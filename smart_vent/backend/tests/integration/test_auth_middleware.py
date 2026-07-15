"""Integration tests for the auth middleware boundary (Issue #373, Phase 2).

Drives the whole `middlewares=[security_headers, auth, csrf]` chain against a real
aiohttp TestServer. The TestServer binds 127.0.0.1, so a request's peer IP is
always 127.0.0.1; a test simulates *ingress* by pointing `app["supervisor_ip"]`
at that loopback address (and sending the `X-Remote-User-Id` header), and a
*direct-port* request by leaving `supervisor_ip` None — exactly the
`is_ingress_request` contract from backend/auth.py.

`require_auth` / `supervisor_ip` are set on the app *before* the server starts
(via the `make_client` factory) so the app is still unfrozen — no post-start
mutation, no deprecation noise. The security-critical property mirrored here at
the HTTP layer: a direct-port caller who forges the ingress headers is rejected
(401), while genuine ingress and a valid session cookie are admitted.
"""

from __future__ import annotations

from collections.abc import Callable

from aiohttp.test_utils import TestClient

from backend import session

INGRESS_HDR = {"X-Remote-User-Id": "ha-user-1"}
LOOPBACK = "127.0.0.1"  # the address the TestServer peer connects from


def _valid_cookie(client: TestClient) -> dict[str, str]:
    tok = session.issue_token(client.app["session_secret"], "logged-in-user")
    return {session.COOKIE_NAME: tok}


# --------------------------------------------------------------------------
# Flag OFF — legacy open behavior (the critical Phase-2 pass-through)
# --------------------------------------------------------------------------


async def test_flag_off_passthrough(client: TestClient) -> None:
    """Default build (require_auth resolves False with no env) → nothing 401s."""
    assert client.app["require_auth"] is False
    resp = await client.get("/api/system/status")
    assert resp.status == 200
    assert (await resp.json())["require_auth"] is False


async def test_flag_off_ignores_forged_ingress_headers(client: TestClient) -> None:
    """With auth off, forged ingress headers change nothing (still open)."""
    resp = await client.get("/api/rooms", headers={"X-Remote-User-Id": "admin"})
    assert resp.status == 200


async def test_flag_off_post_still_open(client: TestClient) -> None:
    resp = await client.post(
        "/api/rooms", json={"name": "Den", "thermostat_entity_id": "climate.den"}
    )
    assert resp.status == 201


# --------------------------------------------------------------------------
# Flag ON — direct port requires a credential
# --------------------------------------------------------------------------


async def test_flag_on_direct_get_401(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    resp = await client.get("/api/rooms")
    assert resp.status == 401
    assert (await resp.json())["error"] == "Authentication required"


async def test_flag_on_direct_post_401(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    resp = await client.post(
        "/api/rooms", json={"name": "Den", "thermostat_entity_id": "climate.den"}
    )
    assert resp.status == 401


async def test_flag_on_401_carries_security_headers(make_client: Callable) -> None:
    """Proves middleware ordering: security_headers wraps the auth 401."""
    client = await make_client(require_auth=True)
    resp = await client.get("/api/rooms")
    assert resp.status == 401
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Content-Security-Policy"]


# --------------------------------------------------------------------------
# Flag ON — the spoofing trap (forged headers from a non-Supervisor peer)
# --------------------------------------------------------------------------


async def test_flag_on_spoofed_ingress_headers_rejected(make_client: Callable) -> None:
    """THE #373 case at the HTTP layer: a direct-port caller sets the ingress
    headers itself, but no Supervisor is resolved → still 401."""
    client = await make_client(require_auth=True, supervisor_ip=None)
    resp = await client.get(
        "/api/rooms",
        headers={
            "X-Remote-User-Id": "admin",
            "X-Ingress-Path": "/api/hassio_ingress/whatever",
            "X-Hass-Source": "core.ingress",
        },
    )
    assert resp.status == 401


async def test_flag_on_spoofed_headers_wrong_peer_rejected(make_client: Callable) -> None:
    """Supervisor resolves to a NON-loopback address, so the loopback TestServer
    peer never matches — forged headers can't fake the transport."""
    client = await make_client(require_auth=True, supervisor_ip="172.30.32.2")
    resp = await client.get("/api/rooms", headers={"X-Remote-User-Id": "admin"})
    assert resp.status == 401


# --------------------------------------------------------------------------
# Flag ON — genuine ingress is auto-trusted (never broken)
# --------------------------------------------------------------------------


async def test_flag_on_ingress_get_200(make_client: Callable) -> None:
    client = await make_client(require_auth=True, supervisor_ip=LOOPBACK)
    resp = await client.get("/api/rooms", headers=INGRESS_HDR)
    assert resp.status == 200


async def test_flag_on_ingress_post_200(make_client: Callable) -> None:
    """A mutating ingress request also passes — but must still satisfy CSRF, and
    real ingress carries X-Ingress-Path (a CSRF-exempt header)."""
    client = await make_client(require_auth=True, supervisor_ip=LOOPBACK)
    resp = await client.post(
        "/api/rooms",
        json={"name": "Den", "thermostat_entity_id": "climate.den"},
        headers={**INGRESS_HDR, "X-Ingress-Path": "/api/hassio_ingress/tok"},
    )
    assert resp.status == 201


async def test_ingress_without_user_header_is_direct(make_client: Callable) -> None:
    """Supervisor peer but no ingress user header (e.g. a watchdog probe) is not
    ingress → still requires a credential."""
    client = await make_client(require_auth=True, supervisor_ip=LOOPBACK)
    resp = await client.get("/api/rooms")  # no X-Remote-User-Id
    assert resp.status == 401


# --------------------------------------------------------------------------
# Flag ON — valid direct-port session cookie is admitted
# --------------------------------------------------------------------------


async def test_flag_on_valid_session_cookie_200(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    resp = await client.get("/api/rooms", cookies=_valid_cookie(client))
    assert resp.status == 200


async def test_flag_on_invalid_session_cookie_401(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    resp = await client.get("/api/rooms", cookies={session.COOKIE_NAME: "garbage.notavalidtoken"})
    assert resp.status == 401


async def test_flag_on_expired_session_cookie_401(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    stale = session.issue_token(client.app["session_secret"], "u", now=1000, ttl=1)
    resp = await client.get("/api/rooms", cookies={session.COOKIE_NAME: stale})
    assert resp.status == 401


# --------------------------------------------------------------------------
# Flag ON — exempt paths stay reachable
# --------------------------------------------------------------------------


async def test_flag_on_healthz_exempt(make_client: Callable) -> None:
    """Health probe must never 401 or the container is marked unhealthy."""
    client = await make_client(require_auth=True)
    resp = await client.get("/api/healthz")
    assert resp.status == 200
    assert (await resp.json())["ok"] is True


# OIDC single sign-on (#464) is off in these tests (no provider configured), so
# auth_status reports the three OIDC fields as disabled/empty.
_OIDC_OFF = {"oidc_enabled": False, "oidc_provider_name": "", "oidc_login_url": ""}


async def test_flag_on_auth_status_public(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    resp = await client.get("/api/auth/status")
    assert resp.status == 200
    assert await resp.json() == {
        "require_auth": True,
        "authenticated": False,
        "method": "none",
        **_OIDC_OFF,
    }


async def test_auth_status_authenticated_via_ingress(make_client: Callable) -> None:
    client = await make_client(require_auth=True, supervisor_ip=LOOPBACK)
    resp = await client.get("/api/auth/status", headers=INGRESS_HDR)
    assert await resp.json() == {
        "require_auth": True,
        "authenticated": True,
        "method": "ingress",
        **_OIDC_OFF,
    }


async def test_auth_status_authenticated_via_session(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    resp = await client.get("/api/auth/status", cookies=_valid_cookie(client))
    body = await resp.json()
    assert body["authenticated"] is True
    assert body["method"] == "session"


async def test_auth_status_reports_off_when_flag_off(client: TestClient) -> None:
    resp = await client.get("/api/auth/status")
    # Auth off ⇒ everyone is effectively authenticated.
    assert await resp.json() == {
        "require_auth": False,
        "authenticated": True,
        "method": "open",
        **_OIDC_OFF,
    }


# --------------------------------------------------------------------------
# Flag ON — in-process MCP loopback (internal token) is trusted
# --------------------------------------------------------------------------


async def test_flag_on_internal_token_trusted(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    resp = await client.get(
        "/api/rooms", headers={"X-Plenum-Internal": client.app["internal_token"]}
    )
    assert resp.status == 200


async def test_flag_on_wrong_internal_token_rejected(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    resp = await client.get("/api/rooms", headers={"X-Plenum-Internal": "not-the-token"})
    assert resp.status == 401


# --------------------------------------------------------------------------
# Flag ON — the /ws stream is guarded
# --------------------------------------------------------------------------


async def test_flag_on_ws_requires_auth(make_client: Callable) -> None:
    """A WS upgrade with no credential is rejected before the handler runs."""
    client = await make_client(require_auth=True)
    resp = await client.get("/ws", headers={"Upgrade": "websocket"})
    assert resp.status == 401


# --------------------------------------------------------------------------
# system/status reflects the flag
# --------------------------------------------------------------------------


async def test_system_status_reports_require_auth(make_client: Callable) -> None:
    client = await make_client(require_auth=True, supervisor_ip=LOOPBACK)
    resp = await client.get("/api/system/status", headers=INGRESS_HDR)
    assert (await resp.json())["require_auth"] is True
