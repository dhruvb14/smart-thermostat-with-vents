"""Security tests for the database restore endpoint."""

import pytest
from aiohttp import FormData


@pytest.mark.asyncio
async def test_restore_too_large_rejected(client):
    """Verify that a database restore exceeding 10MB is rejected (DoS prevention)."""
    # 10MB + 1 byte
    large_data = b"a" * (10 * 1024 * 1024 + 1)

    data = FormData()
    data.add_field('file',
                   large_data,
                   filename='app.db',
                   content_type='application/octet-stream')

    resp = await client.post("/api/restore", data=data)
    assert resp.status == 400
    result = await resp.json()
    assert "exceeds" in result["error"].lower() or "too large" in result["error"].lower()

@pytest.mark.asyncio
async def test_restore_normal_size_accepted(client):
    """Verify that a normal-sized valid database restore still works."""
    # First take a backup to get a valid small DB
    backup_resp = await client.get("/api/backup")
    assert backup_resp.status == 200
    db_bytes = await backup_resp.read()

    data = FormData()
    data.add_field('file',
                   db_bytes,
                   filename='app.db',
                   content_type='application/octet-stream')

    resp = await client.post("/api/restore", data=data)
    assert resp.status == 200
    result = await resp.json()
    assert result["restored"] is True
