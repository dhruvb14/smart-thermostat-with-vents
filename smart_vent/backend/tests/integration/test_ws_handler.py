"""
Integration tests for WSManager.handle() — the WebSocket endpoint at /ws.
These cover the ws_handler.py handle() method lines that aren't exercised
by any other test.
"""

from __future__ import annotations

import contextlib
import os
import tempfile

import pytest
from aiohttp.test_utils import TestClient, TestServer

from backend.main import build_app

from .fake_ha import FakeHomeAssistant


@pytest.fixture
async def client():
    fake_ha = FakeHomeAssistant()
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        app = build_app(fake_ha, db_path, frontend_dist=None, start_ha=False)
        server = TestServer(app)
        async with TestClient(server) as c:
            await c.start_server()
            yield c
    finally:
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            with contextlib.suppress(OSError):
                os.unlink(p)


class TestWSEndpoint:
    async def test_ws_connect_and_close(self, client):
        """Connecting to /ws and closing exercises WSManager.handle()."""
        # Real browsers send Origin on WS handshakes; the CSRF middleware
        # (#373) requires a same-origin value, so mirror that here.
        origin = str(client.make_url("")).rstrip("/")
        async with client.ws_connect("/ws", origin=origin) as ws:
            # Immediately close — exercises the handle() connect/finally path
            await ws.close()

    async def test_ws_receives_broadcast(self, client):
        """A broadcast from the scheduler reaches connected WS clients."""
        import json

        origin = str(client.make_url("")).rstrip("/")
        async with client.ws_connect("/ws", origin=origin) as ws:
            ws_manager = client.app["ws_manager"]
            await ws_manager.broadcast("test_event", {"hello": "world"})
            msg = await ws.receive()
            data = json.loads(msg.data)
            assert data["type"] == "test_event"
            assert data["data"]["hello"] == "world"
            await ws.close()
