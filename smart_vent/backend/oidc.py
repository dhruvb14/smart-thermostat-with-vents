"""OpenID Connect single sign-on for the Plenum web UI (Issue #464).

Follow-up to the authentication work in #373. The direct-port login shipped in
#373 validates a Home Assistant username + password against the Supervisor
``/auth`` backend. That path has two gaps:

* it validates the **first factor only** — HA's MFA/TOTP step runs in the
  interactive ``login_flow``, not the add-on ``/auth`` backend, so a user with
  2FA enabled can sign in with just a password; and
* it **requires a Supervisor**, so standalone / plain-Docker installs (no
  Supervisor) get a 503 and a locked-out UI when ``require_auth`` is on.

This module closes both by adding an **Authorization Code + PKCE** login against
an external OpenID Connect provider the operator configures. The IdP enforces
MFA (gap 1) and needs no Supervisor (gap 2).

Design constraints (carried over from the #373 campaign and the #464 decision):

* **Web UI only.** MCP keeps its minted bearer tokens; ``docs/mcp.md`` documents
  fronting the MCP port with an OIDC proxy. Nothing here touches ``/mcp``.
* **Configured entirely outside the Plenum UI** — add-on options (the Supervisor
  Configuration tab) and/or container env vars, read at boot. There is no in-app
  OIDC setup screen, so an unauthenticated visitor can never reach configuration
  and you never have to disable auth to reach the UI off-HAOS.
* **When OIDC is configured it REPLACES the password path.** The HA
  username/password login route refuses with 403 (see ``routes.login``) so the
  weaker first-factor-only login cannot be reached even by someone who knows the
  endpoint.
* **No server-side session table.** The short-lived login-flow state (CSRF
  ``state`` + PKCE ``code_verifier`` + ``nonce``) rides in an HMAC-signed cookie
  (``session.issue_signed_blob``); on success we mint the same stateless
  ``plenum_session`` cookie the password path uses. A ``/api/backup`` download
  still cannot leak a live session or the signing key.

The IdP is trusted to authenticate the user and enforce MFA. Authorization —
*which* authenticated users may enter — is a simple glob over the identity
(email, then ``preferred_username``, then ``sub``), defaulting to ``*``
(everyone the IdP admits), mirroring the ``OIDC_ALLOWED_USERS_GLOB`` knob already
documented for the MCP auth proxy.

Libraries: **Authlib** provides the OAuth primitives (a CSPRNG ``state`` /
``nonce`` / PKCE ``verifier`` via ``generate_token`` and the RFC 7636 S256
``create_s256_code_challenge``); **joserfc** (the maintained successor to the
now-deprecated ``authlib.jose`` submodule, by the same author) does the
security-critical ID-token signature + claims validation. We deliberately avoid
``authlib.jose`` so nothing depends on a deprecated module.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp
from authlib.common.security import generate_token
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from joserfc import jwt
from joserfc.jwk import KeySet

log = logging.getLogger(__name__)

# Path the IdP redirects back to after login. Combined with the operator's
# configured external base URL to form the exact ``redirect_uri`` registered at
# the IdP. Kept in sync with the route registered in ``routes.py``.
CALLBACK_PATH = "/api/auth/oidc/callback"
LOGIN_PATH = "/api/auth/oidc/login"

# Short-lived cookie carrying the signed login-flow state. SameSite=Lax (NOT
# Strict) so the browser sends it on the top-level redirect back from the IdP,
# which is a cross-site navigation — Strict would drop it and break every login.
STATE_COOKIE = "plenum_oidc_state"
STATE_TTL_SECONDS = 600  # 10 minutes to complete the login round-trip

# How long to wait on the IdP's HTTP endpoints (discovery, JWKS, token).
_HTTP_TIMEOUT_SECONDS = 10

# Minimum scope set. "openid" is mandatory for OIDC; "email"/"profile" give us a
# human identity to allowlist and log.
_DEFAULT_SCOPES = "openid email profile"

# Signing algorithms an ID token is allowed to use. Pinning this allow-list on
# jwt.decode is defense-in-depth against algorithm-substitution / "alg: none"
# downgrade attacks: unsigned and symmetric (HS*) tokens are refused outright, so
# an attacker who holds only the IdP's *public* key can never forge one. These
# are the asymmetric families OIDC providers use in practice.
_ID_TOKEN_ALGS = (
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
    "PS256",
    "PS384",
    "PS512",
)


class OIDCError(Exception):
    """A protocol/validation failure in the OIDC flow (bad state, token
    exchange failed, ID token invalid). Surfaced to the user as a generic
    "login failed" — never with upstream detail (CWE-209)."""


class OIDCForbidden(Exception):
    """The IdP authenticated the user, but they are not on the allowlist. The
    identity is kept for the audit log; the user sees a generic rejection."""

    def __init__(self, identity: str) -> None:
        self.identity = identity
        super().__init__(f"user {identity!r} is not permitted")


@dataclass(frozen=True)
class OIDCConfig:
    """Resolved OIDC configuration. Built from env / add-on options at boot;
    absent (``load_config`` returns ``None``) means OIDC login is off."""

    configuration_url: str  # the IdP's .well-known/openid-configuration
    client_id: str
    client_secret: str
    external_url: str  # public base URL of the web UI (redirect_uri origin)
    scopes: str = _DEFAULT_SCOPES
    allowed_users_glob: str = "*"
    provider_name: str = "SSO"

    @property
    def redirect_uri(self) -> str:
        return self.external_url.rstrip("/") + CALLBACK_PATH

    @property
    def app_root(self) -> str:
        return self.external_url.rstrip("/") + "/"


def load_config(env: Mapping[str, str] | None = None) -> OIDCConfig | None:
    """Build an :class:`OIDCConfig` from the environment, or ``None`` if OIDC is
    not (fully) configured.

    The four required values are the discovery URL, client id, client secret and
    the public external URL. If **some but not all** are set we log a warning
    (an operator half-way through configuration) and treat OIDC as off, so a
    partial config never half-enables a broken login. The password path stays in
    place until OIDC is fully configured.
    """
    env = os.environ if env is None else env
    url = env.get("OIDC_CONFIGURATION_URL", "").strip()
    client_id = env.get("OIDC_CLIENT_ID", "").strip()
    client_secret = env.get("OIDC_CLIENT_SECRET", "").strip()
    external_url = env.get("PLENUM_EXTERNAL_URL", "").strip()

    required = {
        "OIDC_CONFIGURATION_URL": url,
        "OIDC_CLIENT_ID": client_id,
        "OIDC_CLIENT_SECRET": client_secret,
        "PLENUM_EXTERNAL_URL": external_url,
    }
    if not all(required.values()):
        if any(required.values()):
            missing = [k for k, v in required.items() if not v]
            log.warning(
                "OIDC is partially configured (missing %s) — OIDC login disabled; "
                "set all of OIDC_CONFIGURATION_URL, OIDC_CLIENT_ID, "
                "OIDC_CLIENT_SECRET, PLENUM_EXTERNAL_URL to enable it",
                ", ".join(missing),
            )
        return None

    scopes = env.get("OIDC_SCOPES", "").strip() or _DEFAULT_SCOPES
    if "openid" not in scopes.split():
        # "openid" is mandatory; a missing one is almost certainly an oversight.
        scopes = "openid " + scopes
    allowed = env.get("OIDC_ALLOWED_USERS_GLOB", "").strip() or "*"
    name = env.get("OIDC_PROVIDER_NAME", "").strip() or "SSO"
    return OIDCConfig(
        configuration_url=url,
        client_id=client_id,
        client_secret=client_secret,
        external_url=external_url,
        scopes=scopes,
        allowed_users_glob=allowed,
        provider_name=name,
    )


class OIDCProvider:
    """Drives the Authorization Code + PKCE flow against one configured IdP.

    Network access is isolated in three small ``async`` seams
    (:meth:`_http_get_json`, :meth:`_http_post_form`) so tests can monkeypatch
    them — the project pins aiohttp >= 3.14, which ``aioresponses`` cannot mock.
    Discovery metadata and the JWKS are fetched once and cached on the instance.
    """

    def __init__(self, config: OIDCConfig) -> None:
        self.config = config
        self._metadata: dict[str, Any] | None = None
        self._jwks: KeySet | None = None

    # --- network seams (monkeypatched in tests) ---------------------------

    async def _http_get_json(self, url: str) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)
        async with (
            aiohttp.ClientSession(timeout=timeout) as http,
            http.get(url) as resp,
        ):
            if resp.status != 200:
                raise OIDCError(f"GET {url} returned HTTP {resp.status}")
            data: dict[str, Any] = await resp.json()
            return data

    async def _http_post_form(self, url: str, data: dict[str, str]) -> tuple[int, dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)
        async with (
            aiohttp.ClientSession(timeout=timeout) as http,
            http.post(url, data=data) as resp,
        ):
            try:
                body = await resp.json()
            except (aiohttp.ContentTypeError, ValueError):
                body = {}
            return resp.status, body

    # --- cached discovery -------------------------------------------------

    async def metadata(self) -> dict[str, Any]:
        if self._metadata is None:
            self._metadata = await self._http_get_json(self.config.configuration_url)
        return self._metadata

    async def _key_set(self) -> KeySet:
        if self._jwks is None:
            md = await self.metadata()
            jwks_uri = md.get("jwks_uri")
            if not jwks_uri:
                raise OIDCError("IdP metadata has no jwks_uri")
            # The JWKS response is a plain dict at runtime; joserfc validates it.
            jwks = await self._http_get_json(jwks_uri)
            self._jwks = KeySet.import_key_set(jwks)  # type: ignore[arg-type]
        return self._jwks

    # --- flow -------------------------------------------------------------

    async def authorization_url(self) -> tuple[str, dict[str, str]]:
        """Build the IdP authorization URL and the login-flow state to persist.

        Returns ``(auth_url, state_blob)`` where ``state_blob`` is the dict the
        caller signs into the short-lived :data:`STATE_COOKIE` (``state`` +
        PKCE ``verifier`` + ``nonce``) and re-checks in the callback.
        """
        md = await self.metadata()
        endpoint = md.get("authorization_endpoint")
        if not endpoint:
            raise OIDCError("IdP metadata has no authorization_endpoint")
        state = generate_token(32)
        nonce = generate_token(32)
        verifier = generate_token(64)
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "scope": self.config.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": create_s256_code_challenge(verifier),
            "code_challenge_method": "S256",
        }
        sep = "&" if "?" in endpoint else "?"
        return f"{endpoint}{sep}{urlencode(params)}", {
            "state": state,
            "verifier": verifier,
            "nonce": nonce,
        }

    async def exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        """Exchange an authorization code for tokens at the token endpoint."""
        md = await self.metadata()
        endpoint = md.get("token_endpoint")
        if not endpoint:
            raise OIDCError("IdP metadata has no token_endpoint")
        status, body = await self._http_post_form(
            endpoint,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config.redirect_uri,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "code_verifier": code_verifier,
            },
        )
        if status != 200:
            # Log the status for diagnosis; never surface the body (may echo the
            # secret or the code back).
            log.warning("OIDC token endpoint returned HTTP %s", status)
            raise OIDCError("token exchange failed")
        return body

    async def validate_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        """Verify the ID token's signature and claims; return the claims dict.

        Fails **closed**: any error from the JOSE layer (bad signature, unknown
        ``kid``, expired, wrong issuer) becomes an :class:`OIDCError`. The
        audience is checked separately to accept the spec-legal list-or-string
        form, and the ``nonce`` is checked against the one we minted, binding the
        token to this browser's flow.
        """
        md = await self.metadata()
        issuer = md.get("issuer")
        if not issuer:
            raise OIDCError("IdP metadata has no issuer")
        keyset = await self._key_set()
        try:
            claims = jwt.decode(id_token, keyset, algorithms=list(_ID_TOKEN_ALGS)).claims
            jwt.JWTClaimsRegistry(
                iss={"essential": True, "value": issuer},
                exp={"essential": True},
            ).validate(claims)
        except Exception as exc:  # joserfc JoseError subclasses, ValueError, …
            raise OIDCError("ID token validation failed") from exc
        # Audience may be a string or a list per the OIDC spec; our client_id
        # must be present. (Validated here rather than via the registry so both
        # forms are accepted.)
        aud = claims.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        if self.config.client_id not in audiences:
            raise OIDCError("ID token audience mismatch")
        if claims.get("nonce") != nonce:
            raise OIDCError("ID token nonce mismatch")
        return dict(claims)

    @staticmethod
    def identity(claims: Mapping[str, Any]) -> str:
        """Human identity to allowlist and log. Prefer email, then
        ``preferred_username``, then the opaque ``sub``."""
        for key in ("email", "preferred_username", "sub"):
            value = claims.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    def is_allowed(self, identity: str) -> bool:
        """True iff *identity* matches the configured allowlist glob. Empty
        identity is never allowed."""
        return bool(identity) and fnmatch.fnmatch(identity, self.config.allowed_users_glob)

    async def complete_login(self, code: str, code_verifier: str, nonce: str) -> str:
        """Run the back half of the flow: exchange the code, validate the ID
        token, enforce the allowlist. Return the authenticated identity.

        Raises :class:`OIDCForbidden` if authenticated-but-not-allowlisted, or
        :class:`OIDCError` on any protocol/validation failure.
        """
        token = await self.exchange_code(code, code_verifier)
        id_token = token.get("id_token")
        if not id_token or not isinstance(id_token, str):
            raise OIDCError("token response has no id_token")
        claims = await self.validate_id_token(id_token, nonce)
        identity = self.identity(claims)
        if not self.is_allowed(identity):
            raise OIDCForbidden(identity)
        return identity
