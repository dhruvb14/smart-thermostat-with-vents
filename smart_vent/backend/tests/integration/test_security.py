"""Security tests for verifying security headers."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_security_headers_present(client) -> None:
    """Verify that essential security headers are present in all responses."""
    resp = await client.get("/api/rooms")
    assert resp.status == 200

    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "Permissions-Policy" in resp.headers
    assert "Content-Security-Policy" in resp.headers


@pytest.mark.asyncio
async def test_websocket_headers_no_runtime_error(client) -> None:
    """Verify that connecting to WebSocket doesn't trigger a RuntimeError in middleware.

    WebSockets are 'prepared' inside the handler, and modifying headers of a
    prepared response normally raises RuntimeError.
    """
    async with client.ws_connect("/ws") as ws:
        # If we get here, it means the handshake succeeded and no 500 was returned
        # due to a RuntimeError in the middleware.
        assert not ws.closed
        await ws.close()


@pytest.mark.asyncio
async def test_security_headers_on_spa_route(client) -> None:
    """Verify security headers are also present on non-API routes (SPA)."""
    # Even if frontend_dist is None in tests, the route is still registered
    # and should return a 200 if we mock the index.html or if it falls through.
    # In integration tests conftest.py, build_app is called with frontend_dist=None.
    # Let's check a non-existent route which should still have headers.
    resp = await client.get("/non-existent")

    assert "X-Content-Type-Options" in resp.headers
    assert "X-Frame-Options" in resp.headers


@pytest.mark.asyncio
async def test_500_security_headers(client) -> None:
    """Verify that security headers are present even on 500 Internal Server Error."""
    # Passing a non-numeric bin_size raises a ValueError in the overshoot
    # histogram handler which is not caught as an HTTPException, thus resulting
    # in a 500. (The /api/logs paging params are validated gracefully now — see
    # Issue #403 — so they no longer serve as a 500 trigger.)
    resp = await client.get(
        "/api/metrics/thermostats/climate.x/overshoot-histogram",
        params={"bin_size": "not-a-number"},
    )
    assert resp.status == 500

    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"


@pytest.mark.asyncio
async def test_hardened_headers_present(client) -> None:
    """Verify the additional hardened headers added by Sentinel are present."""
    resp = await client.get("/api/rooms")
    assert resp.status == 200

    assert resp.headers.get("X-XSS-Protection") == "1; mode=block"
    assert resp.headers.get("Server") == ""
    assert "frame-ancestors 'self'" in resp.headers.get("Content-Security-Policy", "")
