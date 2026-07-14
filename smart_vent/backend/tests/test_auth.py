"""Tests for the ingress trust boundary (Issue #373, auth.py).

The security-critical property: a direct-port caller who forges the Supervisor's
ingress headers must NOT be classified as ingress. Only a caller whose TCP peer
address is the Supervisor AND who carries the ingress user header counts.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend import auth


class _FakeTransport:
    def __init__(self, peer_ip: str | None):
        self._peer = (peer_ip, 12345) if peer_ip is not None else None

    def get_extra_info(self, key: str):
        if key == "peername":
            return self._peer
        return None


def _request(
    *,
    peer_ip: str | None,
    headers: dict[str, str] | None,
    supervisor_ip: str | None,
    transport: bool = True,
):
    """Minimal duck-typed stand-in for aiohttp.web.Request for auth checks."""
    return SimpleNamespace(
        transport=_FakeTransport(peer_ip) if transport else None,
        headers=headers or {},
        app={"supervisor_ip": supervisor_ip},
    )


SUPERVISOR_IP = "172.30.32.2"


def test_genuine_ingress_is_trusted():
    """Peer is the Supervisor AND the user header is present → trusted."""
    req = _request(
        peer_ip=SUPERVISOR_IP,
        headers={"X-Remote-User-Id": "abc123", "X-Ingress-Path": "/api/hassio_ingress/tok"},
        supervisor_ip=SUPERVISOR_IP,
    )
    assert auth.is_ingress_request(req) is True


def test_spoofed_header_from_wrong_peer_is_rejected():
    """THE spoofing case: a direct-port caller sets the ingress user header
    itself, but connects from a non-Supervisor address → NOT trusted."""
    req = _request(
        peer_ip="192.168.1.50",  # some other container / LAN host
        headers={
            "X-Remote-User-Id": "admin",
            "X-Ingress-Path": "/api/hassio_ingress/anything",
            "X-Hass-Source": "core.ingress",
        },
        supervisor_ip=SUPERVISOR_IP,
    )
    assert auth.is_ingress_request(req) is False


def test_supervisor_peer_without_user_header_is_rejected():
    """Supervisor→add-on traffic that is not ingress (e.g. a watchdog probe):
    right peer, but no ingress user header → NOT ingress."""
    req = _request(
        peer_ip=SUPERVISOR_IP,
        headers={},
        supervisor_ip=SUPERVISOR_IP,
    )
    assert auth.is_ingress_request(req) is False


def test_no_supervisor_resolved_means_never_ingress():
    """Local dev / CI: no Supervisor on the network → nothing is ingress, even
    if the headers and a matching-looking peer are present."""
    req = _request(
        peer_ip=SUPERVISOR_IP,
        headers={"X-Remote-User-Id": "abc123"},
        supervisor_ip=None,
    )
    assert auth.is_ingress_request(req) is False


def test_missing_transport_is_not_ingress():
    """A request with no transport (peer unknowable) is never ingress."""
    req = _request(
        peer_ip=None,
        headers={"X-Remote-User-Id": "abc123"},
        supervisor_ip=SUPERVISOR_IP,
        transport=False,
    )
    assert auth.is_ingress_request(req) is False


def test_missing_peername_is_not_ingress():
    """Transport present but no peername → peer unknowable → not ingress."""
    req = _request(
        peer_ip=None,
        headers={"X-Remote-User-Id": "abc123"},
        supervisor_ip=SUPERVISOR_IP,
    )
    assert auth.is_ingress_request(req) is False


def test_resolve_supervisor_ip_returns_none_when_unresolvable(monkeypatch):
    """When the supervisor hostname does not resolve (no Supervisor present),
    resolution returns None rather than raising."""

    def _boom(_host):
        raise OSError("name or service not known")

    monkeypatch.setattr(auth.socket, "gethostbyname", _boom)
    assert auth.resolve_supervisor_ip() is None


def test_resolve_supervisor_ip_returns_address(monkeypatch):
    monkeypatch.setattr(auth.socket, "gethostbyname", lambda _host: SUPERVISOR_IP)
    assert auth.resolve_supervisor_ip() == SUPERVISOR_IP


# ---------------------------------------------------------------------------
# validate_ha_credentials — the Supervisor /auth backend call (Phase 3)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status: int):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _FakeSession:
    """Minimal stand-in for aiohttp.ClientSession that records the POST and
    returns a canned status (aioresponses is incompatible with aiohttp 3.14)."""

    def __init__(self, status: int, capture: dict):
        self._status = status
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    def post(self, url, *, headers=None, json=None):
        self._capture.update(url=url, headers=headers, json=json)
        return _FakeResp(self._status)


def _patch_session(monkeypatch, status: int, capture: dict) -> None:
    monkeypatch.setattr(auth.aiohttp, "ClientSession", lambda **_kw: _FakeSession(status, capture))


async def test_validate_credentials_no_token_raises(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    with pytest.raises(auth.SupervisorUnavailable):
        await auth.validate_ha_credentials("u", "p")


async def test_validate_credentials_200_is_true(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    capture: dict = {}
    _patch_session(monkeypatch, 200, capture)
    assert await auth.validate_ha_credentials("alice", "pw") is True
    # Request shape: the add-on's Supervisor token + the exact credentials body.
    assert capture["url"] == auth.SUPERVISOR_AUTH_URL
    # The add-on token goes in X-Supervisor-Token. It must NOT be sent in the
    # Authorization header — the /auth endpoint treats Authorization as the
    # user's Basic credentials, so a Bearer there fails every login (regression
    # guard for the live-Supervisor login bug).
    assert capture["headers"]["X-Supervisor-Token"] == "tok"
    assert "Authorization" not in capture["headers"]
    assert capture["json"] == {"username": "alice", "password": "pw"}


async def test_validate_credentials_401_is_false(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    _patch_session(monkeypatch, 401, {})
    assert await auth.validate_ha_credentials("alice", "wrong") is False
