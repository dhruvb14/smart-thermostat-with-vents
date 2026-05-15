"""Security tests for database restore file size limits."""

import io

import aiohttp
import pytest


@pytest.mark.asyncio
async def test_restore_file_too_large(client):
    """Verify that uploading a database larger than 10MB is rejected."""
    # Create a dummy large "file" (10MB + 64KB to ensure it exceeds the limit in the first few chunks)
    # The chunk size in routes.py is 64KB.
    limit = 10 * 1024 * 1024
    large_data = b"0" * (limit + 65536)

    data = aiohttp.FormData()
    data.add_field(
        "file",
        io.BytesIO(large_data),
        filename="too_large.db",
        content_type="application/octet-stream",
    )

    resp = await client.post("/api/restore", data=data)

    assert resp.status == 400
    body = await resp.json()
    assert "error" in body
    assert "File too large" in body["error"]
    assert "max 10MB" in body["error"]


@pytest.mark.asyncio
async def test_restore_missing_file_field(client):
    """Verify that a restore request without the 'file' field is rejected."""
    data = aiohttp.FormData()
    data.add_field("wrong_field", b"some data")

    resp = await client.post("/api/restore", data=data)
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "Multipart field 'file' required"
