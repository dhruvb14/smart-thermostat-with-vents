"""Integration tests for the OIDC single sign-on routes (Issue #464).

Drives the real aiohttp app + middleware chain. ``build_app`` picks up an OIDC
provider by way of a patched ``oidc.load_config``; the provider's flow methods
(``authorization_url`` / ``complete_login``) are stubbed at the class level so no
network is touched (the actual token/JWT logic is unit-tested in
``backend/tests/test_oidc.py``). The state cookie is a real HMAC-signed blob
minted with the app's own session secret, exactly as the login route mints it.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from aiohttp.test_utils import TestClient

from backend import oidc, session

TEST_CONFIG = oidc.OIDCConfig(
    configuration_url="https://idp.example/.well-known/openid-configuration",
    client_id="cid",
    client_secret="sec",
    external_url="https://plenum.example",
    provider_name="TestIdP",
)


@pytest.fixture
def with_oidc(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Make ``build_app`` construct an OIDC provider, with network-free stubs."""
    monkeypatch.setattr(oidc, "load_config", lambda env=None: TEST_CONFIG)

    async def fake_auth_url(self: oidc.OIDCProvider) -> tuple[str, dict[str, str]]:
        return "https://idp.example/authorize?state=S", {
            "state": "S",
            "verifier": "V",
            "nonce": "N",
        }

    async def fake_complete(self: oidc.OIDCProvider, code: str, verifier: str, nonce: str) -> str:
        return "alice@corp.com"

    monkeypatch.setattr(oidc.OIDCProvider, "authorization_url", fake_auth_url)
    monkeypatch.setattr(oidc.OIDCProvider, "complete_login", fake_complete)
    return monkeypatch


def _state_cookie(client: TestClient, *, state: str = "S") -> dict[str, str]:
    blob = session.issue_signed_blob(
        client.app["session_secret"],
        {"state": state, "verifier": "V", "nonce": "N"},
        ttl=oidc.STATE_TTL_SECONDS,
    )
    return {oidc.STATE_COOKIE: blob}


# --------------------------------------------------------------------------- #
# status + password-login interaction
# --------------------------------------------------------------------------- #


async def test_auth_status_reports_oidc(make_client: Callable, with_oidc: object) -> None:
    client = await make_client(require_auth=True)
    body = await (await client.get("/api/auth/status")).json()
    assert body["oidc_enabled"] is True
    assert body["oidc_provider_name"] == "TestIdP"
    assert body["oidc_login_url"] == "/api/auth/oidc/login"


async def test_auth_status_no_oidc_by_default(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    body = await (await client.get("/api/auth/status")).json()
    assert body["oidc_enabled"] is False
    assert body["oidc_provider_name"] == ""
    assert body["oidc_login_url"] == ""


async def test_password_login_disabled_when_oidc(make_client: Callable, with_oidc: object) -> None:
    client = await make_client(require_auth=True)
    resp = await client.post("/api/auth/login", json={"username": "u", "password": "p"})
    assert resp.status == 403
    assert "disabled" in (await resp.json())["error"].lower()


# --------------------------------------------------------------------------- #
# /api/auth/oidc/login
# --------------------------------------------------------------------------- #


async def test_oidc_login_redirects_and_sets_state_cookie(
    make_client: Callable, with_oidc: object
) -> None:
    client = await make_client(require_auth=True)
    resp = await client.get("/api/auth/oidc/login", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"].startswith("https://idp.example/authorize")
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert oidc.STATE_COOKIE in set_cookie
    assert "HttpOnly" in set_cookie
    # Lax (not Strict) so the browser sends it on the cross-site redirect back.
    assert "SameSite=Lax" in set_cookie


async def test_oidc_login_404_when_not_configured(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    resp = await client.get("/api/auth/oidc/login", allow_redirects=False)
    assert resp.status == 404


async def test_oidc_login_502_on_discovery_error(
    make_client: Callable, with_oidc: pytest.MonkeyPatch
) -> None:
    async def boom(self: oidc.OIDCProvider) -> tuple[str, dict[str, str]]:
        raise RuntimeError("discovery unreachable")

    with_oidc.setattr(oidc.OIDCProvider, "authorization_url", boom)
    client = await make_client(require_auth=True)
    resp = await client.get("/api/auth/oidc/login", allow_redirects=False)
    assert resp.status == 502


# --------------------------------------------------------------------------- #
# /api/auth/oidc/callback
# --------------------------------------------------------------------------- #


async def test_oidc_callback_success_authenticates(
    make_client: Callable, with_oidc: object
) -> None:
    client = await make_client(require_auth=True)
    assert (await client.get("/api/rooms")).status == 401  # protected before login
    resp = await client.get(
        "/api/auth/oidc/callback?code=good&state=S",
        cookies=_state_cookie(client),
        allow_redirects=False,
    )
    assert resp.status == 302
    assert resp.headers["Location"] == "https://plenum.example/"
    assert session.COOKIE_NAME in resp.headers.get("Set-Cookie", "")
    # The minted session cookie now authenticates a protected request.
    assert (await client.get("/api/rooms")).status == 200


async def test_oidc_callback_state_mismatch(make_client: Callable, with_oidc: object) -> None:
    client = await make_client(require_auth=True)
    resp = await client.get(
        "/api/auth/oidc/callback?code=good&state=WRONG",
        cookies=_state_cookie(client, state="S"),
        allow_redirects=False,
    )
    assert resp.status == 302
    assert "login_error=sso_state" in resp.headers["Location"]
    assert session.COOKIE_NAME not in resp.headers.get("Set-Cookie", "")


async def test_oidc_callback_no_state_cookie(make_client: Callable, with_oidc: object) -> None:
    client = await make_client(require_auth=True)
    resp = await client.get("/api/auth/oidc/callback?code=good&state=S", allow_redirects=False)
    assert resp.status == 302
    assert "login_error=sso_state" in resp.headers["Location"]


async def test_oidc_callback_idp_error(make_client: Callable, with_oidc: object) -> None:
    client = await make_client(require_auth=True)
    resp = await client.get("/api/auth/oidc/callback?error=access_denied", allow_redirects=False)
    assert resp.status == 302
    assert "login_error=sso_cancelled" in resp.headers["Location"]


async def test_oidc_callback_forbidden(
    make_client: Callable, with_oidc: pytest.MonkeyPatch
) -> None:
    async def forbidden(self: oidc.OIDCProvider, code: str, verifier: str, nonce: str) -> str:
        raise oidc.OIDCForbidden("intruder@evil.com")

    with_oidc.setattr(oidc.OIDCProvider, "complete_login", forbidden)
    client = await make_client(require_auth=True)
    resp = await client.get(
        "/api/auth/oidc/callback?code=good&state=S",
        cookies=_state_cookie(client),
        allow_redirects=False,
    )
    assert resp.status == 302
    assert "login_error=sso_forbidden" in resp.headers["Location"]
    assert session.COOKIE_NAME not in resp.headers.get("Set-Cookie", "")


async def test_oidc_callback_failure(make_client: Callable, with_oidc: pytest.MonkeyPatch) -> None:
    async def fail(self: oidc.OIDCProvider, code: str, verifier: str, nonce: str) -> str:
        raise oidc.OIDCError("token exchange failed")

    with_oidc.setattr(oidc.OIDCProvider, "complete_login", fail)
    client = await make_client(require_auth=True)
    resp = await client.get(
        "/api/auth/oidc/callback?code=good&state=S",
        cookies=_state_cookie(client),
        allow_redirects=False,
    )
    assert resp.status == 302
    assert "login_error=sso_failed" in resp.headers["Location"]


async def test_oidc_callback_404_when_not_configured(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    resp = await client.get("/api/auth/oidc/callback?code=x&state=y", allow_redirects=False)
    assert resp.status == 404
