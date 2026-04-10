"""
WebSocket handler for live UI updates.
Clients connect to /ws and receive JSON push events.
"""
from __future__ import annotations

import asyncio
import json
import logging
import weakref
from typing import Optional

from aiohttp import web, WSMsgType

log = logging.getLogger(__name__)


class WSManager:
    """Tracks connected WebSocket clients and broadcasts events to all of them."""

    def __init__(self) -> None:
        self._clients: weakref.WeakSet[web.WebSocketResponse] = weakref.WeakSet()

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.add(ws)
        log.info("WS client connected (total=%d)", len(self._clients))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
                # Clients don't send commands via WS in this design; ignore
        finally:
            self._clients.discard(ws)
            log.info("WS client disconnected (total=%d)", len(self._clients))
        return ws

    async def broadcast(self, event_type: str, payload: dict) -> None:
        """Push an event to all connected clients."""
        message = json.dumps({"type": event_type, "data": payload})
        dead: list[web.WebSocketResponse] = []
        for ws in list(self._clients):
            try:
                await ws.send_str(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)
