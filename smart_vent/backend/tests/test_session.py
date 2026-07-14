"""Unit tests for the signed session-cookie module (Issue #373, backend/session.py).

Security-critical properties:
* a tampered payload or signature never verifies,
* an expired token never verifies,
* the signing secret is persisted outside app.db and reused across boots.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from backend import session


def _secret() -> bytes:
    return b"0" * 32


def test_issue_then_verify_roundtrip():
    tok = session.issue_token(_secret(), "user-abc", now=1000)
    assert session.verify_token(_secret(), tok, now=1000) == "user-abc"


def test_verify_rejects_wrong_secret():
    tok = session.issue_token(_secret(), "user-abc", now=1000)
    assert session.verify_token(b"1" * 32, tok, now=1000) is None


def test_verify_rejects_tampered_payload():
    tok = session.issue_token(_secret(), "user-abc", now=1000)
    raw, sig = tok.split(".", 1)
    forged_payload = (
        base64.urlsafe_b64encode(
            json.dumps({"sub": "admin", "iat": 1000, "exp": 9999999999}).encode()
        )
        .decode()
        .rstrip("=")
    )
    forged = f"{forged_payload}.{sig}"
    assert session.verify_token(_secret(), forged, now=1000) is None


def test_verify_rejects_expired():
    tok = session.issue_token(_secret(), "user-abc", now=1000, ttl=100)
    # exp = 1100; at now=1100 it is exactly expired (>=).
    assert session.verify_token(_secret(), tok, now=1100) is None
    assert session.verify_token(_secret(), tok, now=1099) == "user-abc"


def test_verify_rejects_malformed_tokens():
    assert session.verify_token(_secret(), "no-dot-here", now=1000) is None
    assert session.verify_token(_secret(), "", now=1000) is None
    # Correct format but the payload is not valid base64/JSON.
    bad_raw = "!!!notb64!!!"
    sig = session._sign(_secret(), bad_raw.encode())
    assert session.verify_token(_secret(), f"{bad_raw}.{sig}", now=1000) is None


def test_verify_rejects_missing_or_bad_exp():
    for payload in ({"sub": "u", "iat": 1}, {"sub": "u", "exp": "soon"}, {"sub": "u", "exp": True}):
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        tok = f"{raw}.{session._sign(_secret(), raw.encode())}"
        assert session.verify_token(_secret(), tok, now=1000) is None


def test_verify_rejects_missing_or_nonstring_sub():
    for payload in ({"exp": 9999999999}, {"sub": 42, "exp": 9999999999}, {"sub": "", "exp": 9}):
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        tok = f"{raw}.{session._sign(_secret(), raw.encode())}"
        assert session.verify_token(_secret(), tok, now=1000) is None


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
