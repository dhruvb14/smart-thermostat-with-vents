"""Regression tests for file size limits on database restore."""

from __future__ import annotations

import pytest
from aiohttp import FormData


@pytest.mark.asyncio
async def test_restore_file_size_limit(client) -> None:
    """Verify that uploading a very large file to /api/restore is rejected."""
    # 11MB of data (limit will be 10MB)
    large_data = b"0" * (11 * 1024 * 1024)

    data = FormData()
    data.add_field("file", large_data, filename="too_big.db")

    resp = await client.post("/api/restore", data=data)

    # We expect a 413 (Payload Too Large) or 400 with a clear error message
    assert resp.status in (400, 413)
    if resp.status == 400:
        body = await resp.json()
        assert "too large" in body["error"].lower()
