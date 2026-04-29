"""Security tests for verifying security headers."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_security_headers_present(client) -> None:
    """Verify that essential security headers are present in all responses."""
    resp = await client.get("/api/rooms")
    assert resp.status == 200

    # These will fail initially until we implement the middleware
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "Permissions-Policy" in resp.headers
    assert "Content-Security-Policy" in resp.headers

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
