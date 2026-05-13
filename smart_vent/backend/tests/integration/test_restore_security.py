"""
Security tests for the database restore endpoint.
Verifies that file size limits are enforced to prevent DoS.
"""

from __future__ import annotations

import aiohttp
import pytest


@pytest.mark.asyncio
async def test_restore_file_size_limit_enforced(client) -> None:
    """Verify that database restore uploads exceeding 10MB are rejected."""
    # 10MB + 1 byte
    oversized_content = b"0" * (10 * 1024 * 1024 + 1)

    data = aiohttp.FormData()
    data.add_field(
        "file",
        oversized_content,
        filename="app.db",
        content_type="application/octet-stream",
    )

    resp = await client.post("/api/restore", data=data)

    # Before the fix, this might succeed (200) or fail with 500 (if invalid SQLite)
    # but it won't be 400 with a "too large" error.
    assert resp.status == 400
    json_data = await resp.json()
    assert "error" in json_data
    assert "too large" in json_data["error"].lower()
