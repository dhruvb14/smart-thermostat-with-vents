"""Security tests for database restore file size limits."""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest
from aiohttp import FormData


@pytest.mark.asyncio
async def test_restore_oversized_file(client) -> None:
    """Verify that a database restore exceeding 10MB is rejected."""
    # Create 11MB of dummy data (MAX_RESTORE_SIZE is 10MB)
    large_data = b"0" * (11 * 1024 * 1024)
    data = FormData()
    data.add_field("file", large_data, filename="too_big.db")

    resp = await client.post("/api/restore", data=data)
    assert resp.status == 400
    json_data = await resp.json()
    assert "Upload too large" in json_data["error"]
    assert "max 10MB" in json_data["error"]


@pytest.mark.asyncio
async def test_restore_invalid_magic_bytes(client) -> None:
    """Verify that a file with invalid SQLite magic bytes is rejected."""
    data = FormData()
    data.add_field("file", b"NOT A SQLITE FILE", filename="invalid.db")

    resp = await client.post("/api/restore", data=data)
    assert resp.status == 400
    json_data = await resp.json()
    assert "not a valid SQLite database" in json_data["error"]


@pytest.mark.asyncio
async def test_restore_valid_file(client) -> None:
    """Verify that a valid small SQLite database is accepted for restore."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # Create a real SQLite database
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        with open(path, "rb") as f:
            valid_data = f.read()

        data = FormData()
        data.add_field("file", valid_data, filename="valid.db")

        resp = await client.post("/api/restore", data=data)
        assert resp.status == 200
        json_data = await resp.json()
        assert json_data["restored"] is True
    finally:
        if os.path.exists(path):
            os.unlink(path)
