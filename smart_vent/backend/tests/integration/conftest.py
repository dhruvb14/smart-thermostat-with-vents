"""
Integration-test fixtures.

Shared machinery for tests that drive the full aiohttp app against a
``FakeHomeAssistant`` and a SQLite file DB. Ticks are invoked manually —
APScheduler's 60-second job loop starts but no tick will actually fire
during a short-lived test, so tests call ``tick()`` / ``tick_engine()``
to advance the state machine.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import AsyncIterator, Callable, Generator

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from backend.main import build_app

from .fake_ha import FakeHomeAssistant


@pytest.fixture
def fake_ha() -> FakeHomeAssistant:
    return FakeHomeAssistant()


@pytest.fixture
def db_path() -> Generator[str, None, None]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        yield path
    finally:
        for suffix in ("", "-wal", "-shm"):
            p = path + suffix
            if os.path.exists(p):
                with contextlib.suppress(OSError):
                    os.unlink(p)


@pytest_asyncio.fixture
async def app(fake_ha: FakeHomeAssistant, db_path: str) -> AsyncIterator[web.Application]:
    application = build_app(fake_ha, db_path, frontend_dist=None, start_ha=False)  # type: ignore[arg-type]
    yield application


@pytest_asyncio.fixture
async def client(app: web.Application) -> AsyncIterator[TestClient]:
    server = TestServer(app)
    async with TestClient(server) as c:
        await c.start_server()
        yield c


@pytest_asyncio.fixture
async def tick(client: TestClient) -> Callable:
    """Return an awaitable that drives one full scheduler tick.

    Depends on ``client`` so the app's startup hook has fired and the
    scheduler has an open DB connection.
    """
    scheduler = client.app["scheduler"]

    async def _tick() -> None:
        await scheduler._tick_all()

    return _tick
