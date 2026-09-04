"""Integration tests for the direct-port login *route* (Issue #373, Phase 3).

The route validates HA credentials via ``auth.validate_ha_credentials`` (which
calls the Supervisor ``/auth`` backend). Here we stub that function to exercise
the route's own branches — success/failure/backend-unavailable/error — and the
cookie round-trip. ``validate_ha_credentials`` itself is unit-tested in
``backend/tests/test_auth.py`` against a fake HTTP session.

(We deliberately do NOT use aioresponses to mock the HTTP layer: aioresponses
0.7.9 is incompatible with aiohttp >= 3.14, which the project pins.)
"""

from __future__ import annotations

from collections.abc import Callable

from aiohttp.test_utils import TestClient

from backend import auth, session


def _stub_validate(
    monkeypatch, *, result: bool | None = None, exc: Exception | None = None
) -> None:
    async def _fake(username: str, password: str) -> bool:
        if exc is not None:
            raise exc
        assert result is not None
        return result

    monkeypatch.setattr(auth, "validate_ha_credentials", _fake)


async def test_login_missing_credentials(client: TestClient) -> None:
    resp = await client.post("/api/auth/login", json={"username": "u"})
    assert resp.status == 400
    assert "required" in (await resp.json())["error"]


async def test_login_invalid_json(client: TestClient) -> None:
    resp = await client.post(
        "/api/auth/login", data="not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status == 400


async def test_login_no_supervisor_returns_503(client: TestClient, monkeypatch) -> None:
    """No SUPERVISOR_TOKEN (dev / plain Docker): the REAL validate_ha_credentials
    raises SupervisorUnavailable → the route returns a clear 503, not a 401."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    resp = await client.post("/api/auth/login", json={"username": "u", "password": "p"})
    assert resp.status == 503
    assert "unavailable" in (await resp.json())["error"].lower()


async def test_login_bad_credentials_401(client: TestClient, monkeypatch) -> None:
    _stub_validate(monkeypatch, result=False)
    resp = await client.post("/api/auth/login", json={"username": "u", "password": "wrong"})
    assert resp.status == 401
    assert (await resp.json())["error"] == "Invalid username or password"


async def test_login_upstream_error_502(client: TestClient, monkeypatch) -> None:
    """A backend error (not SupervisorUnavailable) → generic 502, no leaked detail."""
    _stub_validate(monkeypatch, exc=RuntimeError("boom: sensitive detail"))
    resp = await client.post("/api/auth/login", json={"username": "u", "password": "p"})
    assert resp.status == 502
    body = await resp.json()
    assert body["error"] == "Login failed"  # CWE-209: generic, no upstream text
    assert "boom" not in body["error"]


async def test_login_success_sets_cookie(client: TestClient, monkeypatch) -> None:
    _stub_validate(monkeypatch, result=True)
    resp = await client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    assert resp.status == 200
    assert (await resp.json())["ok"] is True
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert session.COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie
    # The cookie verifies to the logged-in user.
    jar = client.session.cookie_jar.filter_cookies(client.make_url("/"))
    token = jar[session.COOKIE_NAME].value
    assert session.verify_token(client.app["session_secret"], token) == "alice"


async def test_login_cookie_authenticates_protected_request(
    make_client: Callable, monkeypatch
) -> None:
    """End-to-end: login on a require_auth-on stack, then the returned cookie
    lets a protected request through."""
    client = await make_client(require_auth=True)
    _stub_validate(monkeypatch, result=True)
    # Before login: protected endpoint is 401.
    assert (await client.get("/api/rooms")).status == 401
    login = await client.post("/api/auth/login", json={"username": "bob", "password": "pw"})
    assert login.status == 200
    # The TestClient's cookie jar now carries the session → next request passes.
    assert (await client.get("/api/rooms")).status == 200
    assert (await (await client.get("/api/auth/status")).json())["authenticated"] is True


async def test_logout_clears_cookie(make_client: Callable, monkeypatch) -> None:
    client = await make_client(require_auth=True)
    _stub_validate(monkeypatch, result=True)
    await client.post("/api/auth/login", json={"username": "bob", "password": "pw"})
    assert (await client.get("/api/rooms")).status == 200

    logout = await client.post("/api/auth/logout")
    assert logout.status == 200
    assert (await logout.json())["ok"] is True

    # The server must actively expire the cookie — asserting only that a
    # manually-emptied jar gets a 401 would pass even if logout cleared
    # nothing. Pin the Set-Cookie header itself...
    set_cookie = logout.headers.get("Set-Cookie", "")
    assert session.COOKIE_NAME in set_cookie
    assert "Max-Age=0" in set_cookie or "1970" in set_cookie, set_cookie
    # ...and that the client's own jar (which honoured that expiry) no longer
    # authenticates, without us clearing it by hand.
    assert session.COOKIE_NAME not in client.session.cookie_jar.filter_cookies(client.make_url("/"))
    assert (await client.get("/api/rooms")).status == 401


async def test_logout_is_public_without_session(client: TestClient) -> None:
    """Logout is exempt and idempotent — works even with no session at all."""
    resp = await client.post("/api/auth/logout")
    assert resp.status == 200
