"""Unit tests for the signed session-cookie module (Issue #373/#476,
backend/session.py — HS256 JWT via joserfc).

Security-critical properties:
* a tampered token or a token signed by the wrong secret never verifies,
* an expired token never verifies,
* a token claiming a non-HS256 ``alg`` (algorithm confusion) is refused,
* the signing secret is persisted outside app.db and reused across boots.
"""

from __future__ import annotations

from types import SimpleNamespace

from joserfc import jwt as _jwt
from joserfc.jwk import OctKey, RSAKey

from backend import session


def _secret() -> bytes:
    return b"0" * 32


def _mint(claims: dict, *, secret: bytes | None = None) -> str:
    """Mint an HS256 JWT with arbitrary *claims* signed by *secret* — for crafting
    edge-case tokens the public ``issue_*`` helpers won't produce."""
    return _jwt.encode({"alg": "HS256"}, claims, OctKey.import_key(secret or _secret()))


def test_issue_then_verify_roundtrip():
    tok = session.issue_token(_secret(), "user-abc", now=1000)
    assert session.verify_token(_secret(), tok, now=1000) == "user-abc"


def test_verify_rejects_wrong_secret():
    tok = session.issue_token(_secret(), "user-abc", now=1000)
    assert session.verify_token(b"1" * 32, tok, now=1000) is None


def test_verify_rejects_tampered_payload():
    # Flipping any byte of the JWT payload segment breaks the signature.
    tok = session.issue_token(_secret(), "user-abc", now=1000)
    header, payload, sig = tok.split(".")
    tampered_char = "A" if payload[0] != "A" else "B"
    forged = f"{header}.{tampered_char}{payload[1:]}.{sig}"
    assert session.verify_token(_secret(), forged, now=1000) is None


def test_verify_rejects_wrong_algorithm():
    """A token claiming a non-HS256 alg (e.g. RS256) is refused — the
    algorithm-confusion guard restored by pinning algorithms=['HS256']."""
    rsa = RSAKey.generate_key(2048, auto_kid=True)
    rs_tok = _jwt.encode({"alg": "RS256", "kid": rsa.kid}, {"sub": "u", "exp": 9999999999}, rsa)
    assert session.verify_token(_secret(), rs_tok) is None


def test_verify_rejects_expired():
    tok = session.issue_token(_secret(), "user-abc", now=1000, ttl=100)
    # exp = 1100; joserfc treats the token as valid up to and including exp, and
    # expired strictly after it.
    assert session.verify_token(_secret(), tok, now=1100) == "user-abc"
    assert session.verify_token(_secret(), tok, now=1101) is None


def test_verify_rejects_malformed_tokens():
    assert session.verify_token(_secret(), "no-dot-here", now=1000) is None
    assert session.verify_token(_secret(), "", now=1000) is None
    assert session.verify_token(_secret(), "a.b.c", now=1000) is None  # not real JWT segments


def test_verify_rejects_missing_or_bad_exp():
    # Signed by us, but exp missing / non-numeric — verification still refuses it
    # (exp is essential, and joserfc rejects a non-numeric exp).
    for claims in ({"sub": "u", "iat": 1}, {"sub": "u", "exp": "soon"}):
        assert session.verify_token(_secret(), _mint(claims), now=1000) is None


def test_verify_rejects_missing_or_nonstring_sub():
    # All non-expired (exp far in the future) so it's the sub check that rejects.
    for claims in (
        {"exp": 9999999999},
        {"sub": 42, "exp": 9999999999},
        {"sub": "", "exp": 9999999999},
    ):
        assert session.verify_token(_secret(), _mint(claims), now=1000) is None


def test_session_user_reads_cookie():
    secret = _secret()
    tok = session.issue_token(secret, "abc", now=1000)
    req = SimpleNamespace(app={"session_secret": secret}, cookies={session.COOKIE_NAME: tok})
    # verify uses real time; issue a long-lived token so it stays valid.
    tok_live = session.issue_token(secret, "abc")
    req.cookies[session.COOKIE_NAME] = tok_live
    assert session.session_user(req) == "abc"


def test_session_user_none_without_secret_or_cookie():
    assert session.session_user(SimpleNamespace(app={}, cookies={})) is None
    assert (
        session.session_user(SimpleNamespace(app={"session_secret": _secret()}, cookies={})) is None
    )


def test_secret_env_override_wins(monkeypatch, tmp_path):
    """PLENUM_SESSION_SECRET (base64url) overrides the on-disk secret and is not
    written to a file — used to pin the signing key for E2E / multi-replica."""
    monkeypatch.setenv("PLENUM_SESSION_SECRET", session._b64e(b"k" * 32))
    secret = session.load_or_create_secret(str(tmp_path))
    assert secret == b"k" * 32
    assert not (tmp_path / session.SECRET_FILENAME).exists()  # env wins, no file


def test_secret_env_override_invalid_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("PLENUM_SESSION_SECRET", "a")  # not valid base64url
    secret = session.load_or_create_secret(str(tmp_path))
    assert isinstance(secret, bytes) and len(secret) == 32
    assert (tmp_path / session.SECRET_FILENAME).exists()  # fell back to the file


def test_secret_is_persisted_and_reused(tmp_path):
    first = session.load_or_create_secret(str(tmp_path))
    second = session.load_or_create_secret(str(tmp_path))
    assert first == second  # reused, not regenerated
    secret_file = tmp_path / session.SECRET_FILENAME
    assert secret_file.exists()
    # 0600 perms: owner rw only.
    assert (secret_file.stat().st_mode & 0o777) == 0o600


def test_secret_ephemeral_when_read_errors(tmp_path):
    # A non-FileNotFound OSError on read → ephemeral secret, no crash. We trigger
    # it by making the secret PATH a directory, so open(path, "rb") raises
    # IsADirectoryError (an OSError). Deliberately does NOT monkeypatch the global
    # `open` — doing so can disrupt coverage's own file I/O during the run.
    (tmp_path / session.SECRET_FILENAME).mkdir()
    secret = session.load_or_create_secret(str(tmp_path))
    assert isinstance(secret, bytes) and len(secret) == 32


def test_secret_ephemeral_when_write_fails(tmp_path, monkeypatch):
    # File absent (FileNotFound on read) but os.open for write fails → ephemeral.
    def _open_fail(*_a, **_k):
        raise OSError("read-only fs")

    monkeypatch.setattr(session.os, "open", _open_fail)
    secret = session.load_or_create_secret(str(tmp_path))
    assert isinstance(secret, bytes) and len(secret) == 32
    assert not (tmp_path / session.SECRET_FILENAME).exists()


def test_load_secret_ignores_empty_file(tmp_path):
    # A truncated/empty secret file is treated as absent and a new one is written.
    path = tmp_path / session.SECRET_FILENAME
    path.write_bytes(b"")
    secret = session.load_or_create_secret(str(tmp_path))
    assert isinstance(secret, bytes) and len(secret) == 32
    assert path.read_bytes()  # rewritten with content


# --------------------------------------------------------------------------- #
# Signed-blob helpers (used by the OIDC login-flow state cookie, #464)
# --------------------------------------------------------------------------- #


def test_signed_blob_roundtrip():
    data = {"state": "s", "verifier": "v", "nonce": "n"}
    tok = session.issue_signed_blob(_secret(), data, ttl=600, now=1000)
    got = session.verify_signed_blob(_secret(), tok, now=1000)
    assert got == {**data, "iat": 1000, "exp": 1600}


def test_signed_blob_rejects_wrong_secret_and_tamper():
    tok = session.issue_signed_blob(_secret(), {"state": "s"}, ttl=600, now=1000)
    # Signed by us but presented with a different verifying secret → bad signature.
    assert session.verify_signed_blob(b"1" * 32, tok, now=1000) is None
    # Or forged with a different secret entirely.
    forged = _mint({"state": "s", "exp": 9999999999}, secret=b"1" * 32)
    assert session.verify_signed_blob(_secret(), forged, now=1000) is None


def test_signed_blob_rejects_expired():
    tok = session.issue_signed_blob(_secret(), {"state": "s"}, ttl=100, now=1000)
    assert session.verify_signed_blob(_secret(), tok, now=1100)["state"] == "s"  # valid at exp
    assert session.verify_signed_blob(_secret(), tok, now=1101) is None  # expired after exp


def test_signed_blob_rejects_malformed_and_bad_exp():
    assert session.verify_signed_blob(_secret(), "no-dot", now=1000) is None
    assert session.verify_signed_blob(_secret(), "a.b.c", now=1000) is None
    # Correctly signed but exp missing / non-numeric → refused (exp is essential).
    for claims in ({"state": "s"}, {"state": "s", "exp": "later"}):
        assert session.verify_signed_blob(_secret(), _mint(claims), now=1000) is None
