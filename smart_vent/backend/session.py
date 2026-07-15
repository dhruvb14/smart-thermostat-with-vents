"""Signed session cookies for the direct-port web UI (Issue #373).

The browser session is a **stateless, HS256 JWT** carried in an ``HttpOnly`` +
``Secure`` + ``SameSite=Strict`` cookie. The token is minted and verified with
``joserfc`` — the same maintained JOSE library the OIDC login uses (#464/#476) —
signed with a per-install secret; there is deliberately no server-side session
table:

* Nothing session-related is written to ``app.db``, so ``GET /api/backup`` (which
  streams the entire database) cannot exfiltrate live sessions *or* the signing
  key — a concern called out explicitly in #373.
* Verification is a constant-time signature check plus an expiry check (both
  inside joserfc), with no DB round-trip on the hot path. Decoding pins
  ``algorithms=["HS256"]`` so a token claiming any other ``alg`` (``none`` or an
  asymmetric family) is refused — the classic JWT algorithm-confusion guard.

The signing key is a per-install random secret persisted to a file **outside the
database** (``<data-dir>/.session_secret``, mode ``0600``). Keeping it out of
``app.db`` is what makes a leaked backup useless for forging sessions. It
survives restarts (a restart therefore does not log everyone out); if the data
directory is not writable we fall back to an ephemeral in-process secret rather
than crash — sessions then simply don't outlive the process.

This module owns credential *verification and cookie mechanics only*. Credential
*issuance* (validating a login against the Home Assistant ``/auth`` backend, or
the OIDC flow) is the caller's job; this module just mints/verifies the cookie.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
import time
from typing import Any

from aiohttp import web
from joserfc import jwt
from joserfc.jwk import OctKey

log = logging.getLogger(__name__)

# The session/state cookies are symmetric (HMAC) JWTs. Pinning the algorithm on
# decode is what keeps an attacker from presenting a token with a different
# ``alg`` header (``none``, or an asymmetric family) to bypass verification.
_JWT_ALG = "HS256"

# Cookie the browser presents on every same-origin request once logged in.
COOKIE_NAME = "plenum_session"
# How long a direct-port login stays valid before re-authentication.
SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 days
# Secret file lives next to app.db but is NOT part of the DB (backup-safe).
SECRET_FILENAME = ".session_secret"


def _b64e(raw: bytes) -> str:
    """URL-safe base64 without padding (cookie-safe, no ``=`` to escape)."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    """Inverse of :func:`_b64e`; restores the stripped ``=`` padding."""
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def load_or_create_secret(data_dir: str) -> bytes:
    """Return the HMAC signing secret for *data_dir*, creating it once if absent.

    An explicit ``PLENUM_SESSION_SECRET`` env var (base64url of the raw key bytes)
    wins over the on-disk secret. That lets several replicas share one signing key
    so sessions are valid across all of them, and lets an E2E harness pin the key
    to mint a valid cookie. An invalid value is ignored (a warning is logged and
    the per-install file secret is used).

    Otherwise the secret is persisted with ``0600`` perms in a file beside the DB
    (see module docstring). If the file cannot be read or written the process
    still starts — it just uses an ephemeral secret, so sessions don't survive a
    restart. Never raises.
    """
    env_secret = os.environ.get("PLENUM_SESSION_SECRET", "").strip()
    if env_secret:
        try:
            return _b64d(env_secret)
        except ValueError:
            log.warning("PLENUM_SESSION_SECRET is not valid base64url — ignoring it")

    path = os.path.join(data_dir, SECRET_FILENAME)
    try:
        with open(path, "rb") as f:
            stored = f.read().strip()
        if stored:
            return _b64d(stored.decode("ascii"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        log.warning("Could not read the session secret at %s; using an ephemeral one", path)
        return secrets.token_bytes(32)

    secret = secrets.token_bytes(32)
    try:
        # O_EXCL-free but perms-from-creation: 0600 so the secret is never
        # briefly world-readable between create and chmod.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(_b64e(secret).encode("ascii"))
    except OSError:
        log.warning("Could not persist the session secret at %s; using an ephemeral one", path)
    return secret


def issue_signed_blob(
    secret: bytes, data: dict[str, Any], *, ttl: int, now: float | None = None
) -> str:
    """Mint a signed, expiring HS256 JWT carrying an arbitrary claims dict.

    The token's claims are the caller's *data* plus ``iat``/``exp``. Used for the
    short-lived OIDC login-flow cookie (Issue #464), which carries the CSRF
    ``state`` + PKCE ``code_verifier`` + ``nonce``; :func:`issue_token` is the
    session-cookie specialisation (``sub`` only). Signed with *secret* via
    joserfc, so nothing is written to ``app.db`` and a backup can't forge it.
    """
    issued = int(now if now is not None else time.time())
    claims = {**data, "iat": issued, "exp": issued + int(ttl)}
    return jwt.encode({"alg": _JWT_ALG}, claims, OctKey.import_key(secret))


def verify_signed_blob(
    secret: bytes, token: str, *, now: float | None = None
) -> dict[str, Any] | None:
    """Return the decoded claims dict iff *token* is a well-formed HS256 JWT,
    correctly signed by *secret*, and unexpired; ``None`` otherwise.

    Fails **closed**: any joserfc error (bad signature, wrong ``alg``, malformed,
    missing/invalid ``exp``) yields ``None``. ``exp`` is required. Pass *now* to
    pin the clock in tests.
    """
    try:
        claims = jwt.decode(token, OctKey.import_key(secret), algorithms=[_JWT_ALG]).claims
        registry = jwt.JWTClaimsRegistry(
            now=int(now) if now is not None else None,
            exp={"essential": True},
        )
        registry.validate(claims)
    except Exception:
        return None
    return dict(claims)


def issue_token(
    secret: bytes,
    user_id: str,
    *,
    now: float | None = None,
    ttl: int = SESSION_TTL_SECONDS,
) -> str:
    """Mint a signed, expiring session token (HS256 JWT) for *user_id*.

    The session cookie is just an :func:`issue_signed_blob` with a single ``sub``
    claim; :func:`verify_token` reads it back.
    """
    return issue_signed_blob(secret, {"sub": user_id}, ttl=ttl, now=now)


def verify_token(secret: bytes, token: str, *, now: float | None = None) -> str | None:
    """Return the ``sub`` (user id) iff *token* is a valid, unexpired session
    JWT with a non-empty string ``sub``; ``None`` otherwise."""
    claims = verify_signed_blob(secret, token, now=now)
    if claims is None:
        return None
    sub = claims.get("sub")
    return sub if isinstance(sub, str) and sub else None


def session_user(request: web.Request) -> str | None:
    """The authenticated user id from the request's session cookie, or ``None``.

    Reads the signing secret cached on the app at boot. Returns ``None`` when
    there is no secret, no cookie, or the cookie fails verification — the caller
    treats all three the same (unauthenticated).
    """
    secret = request.app.get("session_secret")
    if not secret:
        return None
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return verify_token(secret, token)


def set_session_cookie(
    response: web.StreamResponse, secret: bytes, user_id: str, *, secure: bool
) -> None:
    """Attach a fresh signed session cookie to *response*.

    ``HttpOnly`` keeps it out of JS (XSS can't read it — CSP is already locked
    down in ``main.py``); ``SameSite=Strict`` blocks cross-site sending;
    ``Secure`` is set whenever the request is over TLS. Over plain HTTP the
    cookie cannot be ``Secure`` (the browser would then never send it), so
    direct-port auth should be run behind TLS — see ``docs/auth.md``.
    """
    response.set_cookie(
        COOKIE_NAME,
        issue_token(secret, user_id),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="Strict",
        path="/",
    )


def clear_session_cookie(response: web.StreamResponse) -> None:
    """Expire the session cookie (logout)."""
    response.del_cookie(COOKIE_NAME, path="/")
