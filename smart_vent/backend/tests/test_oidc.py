"""Unit tests for OIDC single sign-on (Issue #464).

Network is isolated behind ``OIDCProvider``'s ``_http_get_json`` /
``_http_post_form`` seams. Most tests pre-populate the discovery + JWKS caches
(no network); the seams themselves are covered against a tiny local aiohttp test
server. ID-token crypto is exercised for real via ``joserfc``: we mint a local
RSA key, publish its public JWKS, sign tokens, and assert validation accepts good
tokens and rejects tampered / expired / mis-audienced / wrong-issuer ones.
"""

from __future__ import annotations

import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from backend import oidc

ISSUER = "https://idp.example"
CLIENT_ID = "plenum-client"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def signing_key() -> RSAKey:
    return RSAKey.generate_key(2048, auto_kid=True)


def _config(**over: object) -> oidc.OIDCConfig:
    base: dict[str, object] = {
        "configuration_url": "https://idp.example/.well-known/openid-configuration",
        "client_id": CLIENT_ID,
        "client_secret": "s3cret",
        "external_url": "https://plenum.example",
    }
    base.update(over)
    return oidc.OIDCConfig(**base)  # type: ignore[arg-type]


def _provider(signing_key: RSAKey, **over: object) -> oidc.OIDCProvider:
    """A provider whose discovery + JWKS caches are pre-populated from a local
    key, so no method touches the network."""
    provider = oidc.OIDCProvider(_config(**over))
    provider._metadata = {
        "issuer": ISSUER,
        "authorization_endpoint": ISSUER + "/authorize",
        "token_endpoint": ISSUER + "/token",
        "jwks_uri": ISSUER + "/jwks",
    }
    provider._jwks = KeySet.import_key_set({"keys": [signing_key.as_dict(private=False)]})
    return provider


def _id_token(signing_key: RSAKey, **claims: object) -> str:
    payload: dict[str, object] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "user-123",
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
    }
    payload.update(claims)
    return jwt.encode({"alg": "RS256", "kid": signing_key.kid}, payload, signing_key)


# --------------------------------------------------------------------------- #
# load_config
# --------------------------------------------------------------------------- #


def test_load_config_empty_is_none() -> None:
    assert oidc.load_config({}) is None


def test_load_config_partial_is_none_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    cfg = oidc.load_config({"OIDC_CLIENT_ID": "abc"})  # only one of four
    assert cfg is None
    assert "partially configured" in caplog.text.lower()


def test_load_config_full() -> None:
    cfg = oidc.load_config(
        {
            "OIDC_CONFIGURATION_URL": "https://idp/.well-known/openid-configuration",
            "OIDC_CLIENT_ID": "cid",
            "OIDC_CLIENT_SECRET": "sec",
            "PLENUM_EXTERNAL_URL": "https://plenum.example/",
        }
    )
    assert cfg is not None
    assert cfg.client_id == "cid"
    assert cfg.scopes == "openid email profile"  # default
    assert cfg.allowed_users_glob == "*"  # default
    assert cfg.provider_name == "SSO"  # default
    # redirect_uri / app_root strip the trailing slash exactly once.
    assert cfg.redirect_uri == "https://plenum.example/api/auth/oidc/callback"
    assert cfg.app_root == "https://plenum.example/"


def test_load_config_injects_openid_scope() -> None:
    cfg = oidc.load_config(
        {
            "OIDC_CONFIGURATION_URL": "https://idp/x",
            "OIDC_CLIENT_ID": "cid",
            "OIDC_CLIENT_SECRET": "sec",
            "PLENUM_EXTERNAL_URL": "https://plenum",
            "OIDC_SCOPES": "email groups",  # missing openid
        }
    )
    assert cfg is not None
    assert cfg.scopes.split()[0] == "openid"
    assert "email" in cfg.scopes and "groups" in cfg.scopes


def test_load_config_custom_name_and_allowlist() -> None:
    cfg = oidc.load_config(
        {
            "OIDC_CONFIGURATION_URL": "https://idp/x",
            "OIDC_CLIENT_ID": "cid",
            "OIDC_CLIENT_SECRET": "sec",
            "PLENUM_EXTERNAL_URL": "https://plenum",
            "OIDC_PROVIDER_NAME": "Authelia",
            "OIDC_ALLOWED_USERS_GLOB": "*@corp.com",
        }
    )
    assert cfg is not None
    assert cfg.provider_name == "Authelia"
    assert cfg.allowed_users_glob == "*@corp.com"


# --------------------------------------------------------------------------- #
# identity / allowlist
# --------------------------------------------------------------------------- #


def test_identity_precedence() -> None:
    ident = oidc.OIDCProvider.identity  # staticmethod — no instance needed
    assert ident({"email": "e", "preferred_username": "u", "sub": "s"}) == "e"
    assert ident({"preferred_username": "u", "sub": "s"}) == "u"
    assert ident({"sub": "s"}) == "s"
    assert ident({}) == ""
    assert ident({"email": ""}) == ""  # empty skipped


@pytest.mark.parametrize(
    "glob,identity,allowed",
    [
        ("*", "anyone@x.com", True),
        ("*", "", False),  # empty never allowed
        ("*@corp.com", "a@corp.com", True),
        ("*@corp.com", "a@evil.com", False),
        ("alice@corp.com", "alice@corp.com", True),
        ("alice@corp.com", "bob@corp.com", False),
    ],
)
def test_is_allowed(signing_key: RSAKey, glob: str, identity: str, allowed: bool) -> None:
    provider = _provider(signing_key, allowed_users_glob=glob)
    assert provider.is_allowed(identity) is allowed


# --------------------------------------------------------------------------- #
# authorization_url
# --------------------------------------------------------------------------- #


async def test_authorization_url(signing_key: RSAKey) -> None:
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlparse

    provider = _provider(signing_key)
    url, blob = await provider.authorization_url()
    assert url.startswith(ISSUER + "/authorize?")
    query = parse_qs(urlparse(url).query)
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [provider.config.redirect_uri]
    assert set(blob) == {"state", "verifier", "nonce"}
    # The state/nonce in the blob are what the callback re-checks.
    assert query["state"] == [blob["state"]]
    assert query["nonce"] == [blob["nonce"]]
    # The challenge is the unpadded base64url S256 of the verifier we kept.
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(blob["verifier"].encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert query["code_challenge"] == [expected]


async def test_authorization_url_no_endpoint(signing_key: RSAKey) -> None:
    provider = _provider(signing_key)
    provider._metadata = {"issuer": ISSUER}  # no authorization_endpoint
    with pytest.raises(oidc.OIDCError):
        await provider.authorization_url()


# --------------------------------------------------------------------------- #
# validate_id_token
# --------------------------------------------------------------------------- #


async def test_validate_id_token_ok(signing_key: RSAKey) -> None:
    provider = _provider(signing_key)
    claims = await provider.validate_id_token(
        _id_token(signing_key, nonce="N", email="a@corp.com"), "N"
    )
    assert claims["sub"] == "user-123"
    assert claims["email"] == "a@corp.com"


async def test_validate_id_token_aud_list(signing_key: RSAKey) -> None:
    provider = _provider(signing_key)
    token = _id_token(signing_key, nonce="N", aud=["someone-else", CLIENT_ID])
    claims = await provider.validate_id_token(token, "N")
    assert claims["sub"] == "user-123"


async def test_validate_id_token_wrong_audience(signing_key: RSAKey) -> None:
    provider = _provider(signing_key)
    token = _id_token(signing_key, nonce="N", aud="not-us")
    with pytest.raises(oidc.OIDCError):
        await provider.validate_id_token(token, "N")


async def test_validate_id_token_wrong_issuer(signing_key: RSAKey) -> None:
    provider = _provider(signing_key)
    token = _id_token(signing_key, nonce="N", iss="https://evil")
    with pytest.raises(oidc.OIDCError):
        await provider.validate_id_token(token, "N")


async def test_validate_id_token_expired(signing_key: RSAKey) -> None:
    provider = _provider(signing_key)
    token = _id_token(signing_key, nonce="N", exp=int(time.time()) - 5)
    with pytest.raises(oidc.OIDCError):
        await provider.validate_id_token(token, "N")


async def test_validate_id_token_nonce_mismatch(signing_key: RSAKey) -> None:
    provider = _provider(signing_key)
    token = _id_token(signing_key, nonce="N")
    with pytest.raises(oidc.OIDCError):
        await provider.validate_id_token(token, "DIFFERENT")


async def test_validate_id_token_wrong_key(signing_key: RSAKey) -> None:
    """A token signed by a different key (unknown kid) must be rejected."""
    provider = _provider(signing_key)
    attacker = RSAKey.generate_key(2048, auto_kid=True)
    token = _id_token(attacker, nonce="N")
    with pytest.raises(oidc.OIDCError):
        await provider.validate_id_token(token, "N")


async def test_validate_id_token_rejects_symmetric_alg(signing_key: RSAKey) -> None:
    """An HS256-signed token must be refused: only asymmetric algorithms are in
    the allow-list, defeating the classic 'sign with the public key as an HMAC
    secret' confusion attack."""
    from joserfc.jwk import OctKey

    provider = _provider(signing_key)
    oct_key = OctKey.import_key("x" * 32)
    token = jwt.encode(
        {"alg": "HS256"},
        {"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "nonce": "N", "exp": int(time.time()) + 600},
        oct_key,
    )
    with pytest.raises(oidc.OIDCError):
        await provider.validate_id_token(token, "N")


# --------------------------------------------------------------------------- #
# exchange_code / complete_login (network seam monkeypatched)
# --------------------------------------------------------------------------- #


async def test_exchange_code_ok(signing_key: RSAKey, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider(signing_key)

    async def fake_post(url: str, data: dict) -> tuple[int, dict]:
        assert data["grant_type"] == "authorization_code"
        assert data["code"] == "the-code"
        return 200, {"id_token": "xyz", "access_token": "a"}

    monkeypatch.setattr(provider, "_http_post_form", fake_post)
    token = await provider.exchange_code("the-code", "verifier")
    assert token["id_token"] == "xyz"


async def test_exchange_code_non_200(signing_key: RSAKey, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider(signing_key)

    async def fake_post(url: str, data: dict) -> tuple[int, dict]:
        return 401, {"error": "invalid_grant"}

    monkeypatch.setattr(provider, "_http_post_form", fake_post)
    with pytest.raises(oidc.OIDCError):
        await provider.exchange_code("bad", "verifier")


async def test_exchange_code_no_endpoint(signing_key: RSAKey) -> None:
    provider = _provider(signing_key)
    provider._metadata = {"issuer": ISSUER}  # no token_endpoint
    with pytest.raises(oidc.OIDCError):
        await provider.exchange_code("c", "v")


async def test_complete_login_ok(signing_key: RSAKey, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider(signing_key)

    async def fake_exchange(code: str, verifier: str) -> dict:
        return {"id_token": _id_token(signing_key, nonce="N", email="a@corp.com")}

    monkeypatch.setattr(provider, "exchange_code", fake_exchange)
    identity = await provider.complete_login("code", "verifier", "N")
    assert identity == "a@corp.com"


async def test_complete_login_forbidden(
    signing_key: RSAKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(signing_key, allowed_users_glob="*@corp.com")

    async def fake_exchange(code: str, verifier: str) -> dict:
        return {"id_token": _id_token(signing_key, nonce="N", email="intruder@evil.com")}

    monkeypatch.setattr(provider, "exchange_code", fake_exchange)
    with pytest.raises(oidc.OIDCForbidden) as excinfo:
        await provider.complete_login("code", "verifier", "N")
    assert excinfo.value.identity == "intruder@evil.com"


async def test_complete_login_missing_id_token(
    signing_key: RSAKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(signing_key)

    async def fake_exchange(code: str, verifier: str) -> dict:
        return {"access_token": "a"}  # no id_token

    monkeypatch.setattr(provider, "exchange_code", fake_exchange)
    with pytest.raises(oidc.OIDCError):
        await provider.complete_login("code", "verifier", "N")


# --------------------------------------------------------------------------- #
# HTTP seams + discovery caching (against a local aiohttp server)
# --------------------------------------------------------------------------- #


@pytest.fixture
async def idp_server(signing_key: RSAKey):
    """A tiny stand-in IdP serving discovery, JWKS and a token endpoint."""
    calls = {"discovery": 0, "jwks": 0}

    async def discovery(request: web.Request) -> web.Response:
        calls["discovery"] += 1
        base = str(request.url.origin())
        return web.json_response(
            {
                "issuer": base,
                "authorization_endpoint": base + "/authorize",
                "token_endpoint": base + "/token",
                "jwks_uri": base + "/jwks",
            }
        )

    async def jwks(request: web.Request) -> web.Response:
        calls["jwks"] += 1
        return web.json_response({"keys": [signing_key.as_dict(private=False)]})

    async def token(request: web.Request) -> web.Response:
        form = await request.post()
        if form.get("code") == "good":
            return web.json_response({"id_token": "signed", "access_token": "a"})
        return web.json_response({"error": "invalid_grant"}, status=400)

    async def notjson(request: web.Request) -> web.Response:
        return web.Response(text="nope", status=500)

    app = web.Application()
    app.router.add_get("/.well-known/openid-configuration", discovery)
    app.router.add_get("/jwks", jwks)
    app.router.add_post("/token", token)
    app.router.add_route("*", "/boom", notjson)  # 500 + non-JSON body, any method
    server = TestServer(app)
    await server.start_server()
    server_url = str(server.make_url(""))
    try:
        yield server, server_url, calls
    finally:
        await server.close()


async def test_http_get_json_and_caching(idp_server) -> None:
    server, base, calls = idp_server
    provider = oidc.OIDCProvider(
        _config(configuration_url=base + "/.well-known/openid-configuration")
    )
    md1 = await provider.metadata()
    md2 = await provider.metadata()  # cached — no second fetch
    assert md1["issuer"] == base.rstrip("/")
    assert calls["discovery"] == 1
    assert md2 is md1
    keyset1 = await provider._key_set()
    await provider._key_set()  # cached
    assert calls["jwks"] == 1
    assert keyset1 is provider._jwks


async def test_http_get_json_non_200(idp_server) -> None:
    server, base, _ = idp_server
    provider = oidc.OIDCProvider(_config())
    with pytest.raises(oidc.OIDCError):
        await provider._http_get_json(base + "/boom")


async def test_http_post_form_roundtrip(idp_server) -> None:
    server, base, _ = idp_server
    provider = oidc.OIDCProvider(_config())
    provider._metadata = {"token_endpoint": base + "/token"}
    provider._jwks = None
    status, body = await provider._http_post_form(base + "/token", {"code": "good"})
    assert status == 200
    assert body["id_token"] == "signed"
    # non-JSON / error body → still returns the status with an empty dict.
    status2, body2 = await provider._http_post_form(base + "/boom", {})
    assert status2 == 500
    assert body2 == {}


async def test_key_set_missing_jwks_uri(signing_key: RSAKey) -> None:
    provider = oidc.OIDCProvider(_config())
    provider._metadata = {"issuer": ISSUER}  # no jwks_uri
    with pytest.raises(oidc.OIDCError):
        await provider._key_set()
