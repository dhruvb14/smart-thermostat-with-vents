"""
Integration tests for WSManager.handle() — the WebSocket endpoint at /ws.
These cover the ws_handler.py handle() method lines that aren't exercised
by any other test.
"""

from __future__ import annotations

import asyncio
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


async def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    """Poll *predicate* until true (the server side of a WS handshake settles a
    tick or two after the client's await returns). Raises on timeout so a
    never-satisfied condition fails loudly instead of passing vacuously."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.002)


class TestWSEndpoint:
    async def test_ws_connect_registers_then_close_deregisters(self, client):
        """handle() must add the socket to the broadcast set on connect and
        discard it in its `finally` on disconnect — a leaked entry would keep
        broadcasting to a dead peer."""
        # Real browsers send Origin on WS handshakes; the CSRF middleware
        # (#373) requires a same-origin value, so mirror that here.
        origin = str(client.make_url("")).rstrip("/")
        manager = client.app["ws_manager"]
        assert len(manager._clients) == 0

        async with client.ws_connect("/ws", origin=origin) as ws:
            await _wait_for(lambda: len(manager._clients) == 1)
            # Hold a strong reference so the WeakSet cannot drop the entry via
            # GC — the discard below then proves handle()'s finally ran.
            server_ws = next(iter(manager._clients))
            await ws.close()

        await _wait_for(lambda: server_ws not in manager._clients)
        assert len(manager._clients) == 0

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
