"""Security tests for the restore endpoint."""

from __future__ import annotations

import io

import pytest


@pytest.mark.asyncio
async def test_restore_too_large_rejected(client):
    """Verify that a restore upload exceeding 10MB is rejected."""
    # 11MB of data
    large_data = b"0" * (11 * 1024 * 1024)
    data = io.BytesIO(large_data)

    # We use a multipart request as expected by the handler
    resp = await client.post("/api/restore", data={"file": data})

    # This should return 400 Bad Request if the limit is enforced.
    assert resp.status == 400
    result = await resp.json()
    assert "exceeds" in result["error"]
