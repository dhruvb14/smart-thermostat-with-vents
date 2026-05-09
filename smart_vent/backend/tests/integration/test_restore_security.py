from __future__ import annotations

import aiohttp
import pytest


@pytest.mark.asyncio
async def test_restore_db_size_limit(client):
    """Verify that uploading a large file to /api/restore is blocked."""
    # 11MB dummy data (exceeds 10MB limit)
    large_data = b"a" * (11 * 1024 * 1024)

    data = aiohttp.FormData()
    data.add_field("file", large_data, filename="large.db")

    resp = await client.post("/api/restore", data=data)

    assert resp.status == 400
    body = await resp.json()
    assert "too large" in body["error"]
    assert "10MB" in body["error"]
