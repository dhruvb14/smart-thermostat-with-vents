"""Security tests for database restore."""

import pytest
from aiohttp import FormData


@pytest.mark.asyncio
async def test_restore_db_size_limit(client):
    """Verify that database restore rejects files larger than 10MB."""
    # 11MB of data starting with SQLite magic bytes
    large_data = b"SQLite format 3\x00" + b"0" * (11 * 1024 * 1024)
    form = FormData()
    form.add_field("file", large_data, filename="large.db")

    resp = await client.post("/api/restore", data=form)

    # We expect a 400 Bad Request when the file is too large
    assert resp.status == 400
    data = await resp.json()
    assert "too large" in data["error"].lower()
