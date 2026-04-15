"""
EventLogger — writes structured events to SQLite and broadcasts them over WebSocket.

Usage:
    logger = EventLogger(broadcast=ws_manager.broadcast)
    logger.set_conn(db_conn)           # call after DB is initialised
    await logger.log("info", "engine", "Cycle started", {"thermostat": "climate.upstairs"})
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime

import aiosqlite

from . import db

log = logging.getLogger(__name__)

BroadcastFn = Callable[[str, dict], Coroutine]


class EventLogger:
    def __init__(self, broadcast: BroadcastFn | None = None) -> None:
        self._broadcast = broadcast
        self._conn: aiosqlite.Connection | None = None

    def set_conn(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def log(
        self,
        level: str,  # 'info' | 'warning' | 'error'
        category: str,  # 'system' | 'api' | 'engine' | 'presence' | 'ha'
        message: str,
        details: dict | None = None,
    ) -> None:
        """Write an event to the DB and push it over WebSocket. Never raises."""
        timestamp = datetime.utcnow().isoformat()
        details_json = json.dumps(details) if details else None
        rowid = 0

        try:
            if self._conn:
                rowid = await db.insert_event_log(
                    self._conn, timestamp, level, category, message, details_json
                )
        except Exception as exc:
            log.warning("EventLogger DB write failed: %s", exc)

        payload = {
            "id": rowid,
            "timestamp": timestamp,
            "level": level,
            "category": category,
            "message": message,
            "details": details,
        }

        try:
            if self._broadcast:
                await self._broadcast("log_event", payload)
        except Exception as exc:
            log.debug("EventLogger broadcast failed: %s", exc)

        # Also emit to Python log so the server console is useful
        py_log = getattr(
            log, level if level in ("info", "warning", "error") else "info"
        )
        py_log("[%s] %s", category.upper(), message)
