"""
Tests for backend/event_logger.py edge cases (error branches).

``EventLogger.log`` is documented as "never raises", so several of these cases
have no return value to inspect. They therefore assert on the two things the
method *does* leave behind — the mirrored Python log record and, where a
connection/broadcast is wired up, the row/payload that still made it through —
rather than merely calling the method and letting it pass vacuously.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from backend import db
from backend.event_logger import EventLogger

_LOGGER = "backend.event_logger"


@pytest.fixture
async def conn():
    c = await aiosqlite.connect(":memory:")
    c.row_factory = aiosqlite.Row
    await db.init_db(c)
    try:
        yield c
    finally:
        await c.close()


async def _rows(c: aiosqlite.Connection) -> list[aiosqlite.Row]:
    async with c.execute("SELECT * FROM event_log ORDER BY id") as cur:
        return list(await cur.fetchall())


class TestEventLoggerNoConn:
    async def test_log_without_conn_skips_db_and_still_mirrors_to_python_log(self, caplog):
        """No connection set → the `if self._conn` guard skips the insert
        entirely (so no "DB write failed" warning is emitted) and the Python
        log mirror still runs."""
        logger = EventLogger()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            await logger.log("info", "system", "hello")

        messages = [r.getMessage() for r in caplog.records]
        assert "[SYSTEM] hello" in messages
        assert not [m for m in messages if "DB write failed" in m]

    async def test_log_without_broadcast_skips_push_and_mirrors_at_level(self, caplog):
        """No broadcast callable → the push is skipped (no "broadcast failed"
        debug line) and the mirrored record carries the requested level."""
        logger = EventLogger(broadcast=None)
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            await logger.log("warning", "engine", "something happened", {"k": "v"})

        record = next(r for r in caplog.records if r.getMessage() == "[ENGINE] something happened")
        assert record.levelno == logging.WARNING
        assert not [r for r in caplog.records if "broadcast failed" in r.getMessage()]


class TestEventLoggerDbFailure:
    async def test_db_write_failure_is_swallowed_and_broadcast_still_runs(self, caplog):
        """A failing insert must be logged as a warning and must NOT stop the
        WebSocket push — the Live Feed still shows the event (with id 0)."""
        failing_conn = AsyncMock()
        failing_conn.execute = AsyncMock(side_effect=RuntimeError("db error"))
        received: list[tuple[str, dict]] = []

        async def capture(event_type, payload):
            received.append((event_type, payload))

        logger = EventLogger(broadcast=capture)
        logger.set_conn(failing_conn)
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            await logger.log("error", "system", "db broken")  # must not raise

        assert any("EventLogger DB write failed" in r.getMessage() for r in caplog.records)
        assert len(received) == 1, "the broadcast must still fire after a DB failure"
        assert received[0][1]["id"] == 0, "no rowid was assigned, so the payload carries 0"
        assert received[0][1]["message"] == "db broken"


class TestEventLoggerBroadcastFailure:
    async def test_broadcast_failure_is_swallowed_after_the_row_is_written(self, conn, caplog):
        """The DB write happens before the push, so a broken broadcast must not
        cost us the persisted row — and the failure is logged at DEBUG."""

        async def bad_broadcast(event_type, payload):
            raise RuntimeError("ws error")

        logger = EventLogger(broadcast=bad_broadcast)
        logger.set_conn(conn)
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            await logger.log("info", "system", "msg")  # must not raise

        rows = await _rows(conn)
        assert [r["message"] for r in rows] == ["msg"]
        assert any("EventLogger broadcast failed" in r.getMessage() for r in caplog.records)


class TestEventLoggerBroadcastCalled:
    async def test_broadcast_receives_correct_payload(self, conn):
        received = []

        async def capture(event_type, payload):
            received.append((event_type, payload))

        logger = EventLogger(broadcast=capture)
        logger.set_conn(conn)
        await logger.log("info", "api", "room created", {"room_id": "abc"})

        assert len(received) == 1
        et, payload = received[0]
        assert et == "log_event"
        assert payload["level"] == "info"
        assert payload["category"] == "api"
        assert payload["message"] == "room created"
        assert payload["details"] == {"room_id": "abc"}
        # The payload's id is the real rowid of the row just inserted, not a
        # placeholder — the Live Feed keys off it.
        rows = await _rows(conn)
        assert payload["id"] == rows[0]["id"]
        assert rows[0]["details"] == '{"room_id": "abc"}'


class TestEventLoggerUnknownLevel:
    async def test_unknown_level_falls_back_to_info(self, caplog):
        """An unknown log level must mirror at INFO rather than raising on the
        missing ``log.debug``-style attribute lookup."""
        logger = EventLogger()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            await logger.log("debug", "system", "verbose msg")  # must not raise

        record = next(r for r in caplog.records if r.getMessage() == "[SYSTEM] verbose msg")
        assert record.levelno == logging.INFO


class TestEventLoggerSetConn:
    def test_set_conn_stores_connection(self):
        mock_conn = MagicMock()
        logger = EventLogger()
        logger.set_conn(mock_conn)
        assert logger._conn is mock_conn
