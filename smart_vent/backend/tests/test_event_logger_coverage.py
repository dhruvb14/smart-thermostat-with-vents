"""
Tests for backend/event_logger.py edge cases (error branches).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.event_logger import EventLogger


class TestEventLoggerNoConn:
    async def test_log_without_conn_does_not_raise(self):
        logger = EventLogger()
        await logger.log("info", "system", "hello")

    async def test_log_without_broadcast_does_not_raise(self):
        logger = EventLogger(broadcast=None)
        await logger.log("warning", "engine", "something happened", {"k": "v"})


class TestEventLoggerDbFailure:
    async def test_db_write_failure_is_swallowed(self):
        failing_conn = AsyncMock()
        # Simulate DB write error by making execute raise
        failing_conn.execute = AsyncMock(side_effect=RuntimeError("db error"))

        logger = EventLogger()
        logger._conn = failing_conn
        await logger.log("error", "system", "db broken")  # must not raise


class TestEventLoggerBroadcastFailure:
    async def test_broadcast_failure_is_swallowed(self):
        async def bad_broadcast(event_type, payload):
            raise RuntimeError("ws error")

        logger = EventLogger(broadcast=bad_broadcast)
        await logger.log("info", "system", "msg")  # must not raise


class TestEventLoggerBroadcastCalled:
    async def test_broadcast_receives_correct_payload(self):
        received = []

        async def capture(event_type, payload):
            received.append((event_type, payload))

        logger = EventLogger(broadcast=capture)
        await logger.log("info", "api", "room created", {"room_id": "abc"})

        assert len(received) == 1
        et, payload = received[0]
        assert et == "log_event"
        assert payload["level"] == "info"
        assert payload["category"] == "api"
        assert payload["message"] == "room created"
        assert payload["details"] == {"room_id": "abc"}


class TestEventLoggerUnknownLevel:
    async def test_unknown_level_falls_back_to_info(self):
        """An unknown log level should not raise (falls back to getattr default)."""
        logger = EventLogger()
        await logger.log("debug", "system", "verbose msg")  # must not raise


class TestEventLoggerSetConn:
    def test_set_conn_stores_connection(self):
        mock_conn = MagicMock()
        logger = EventLogger()
        logger.set_conn(mock_conn)
        assert logger._conn is mock_conn
