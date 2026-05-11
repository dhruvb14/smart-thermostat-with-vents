import aiohttp
import pytest


@pytest.mark.asyncio
async def test_restore_db_size_limit(client):
    # Create a large "database" file (11MB)
    large_data = b"SQLite format 3\x00" + b"0" * (11 * 1024 * 1024)

    data = aiohttp.FormData()
    data.add_field("file", large_data, filename="large.db")

    resp = await client.post("/api/restore", data=data)

    # It should fail with 400
    assert resp.status == 400
    body = await resp.json()
    assert "too large" in body["error"]
