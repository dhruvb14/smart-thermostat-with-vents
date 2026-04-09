"""
Home Assistant WebSocket API client.

Responsibilities:
- Maintain a persistent authenticated WebSocket connection to HA
- Subscribe to entity state changes and fire registered callbacks
- Call HA services (climate.set_temperature, cover.open/close_cover)
- Fetch current entity state on demand
- Handle reconnection with exponential backoff
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
from collections import defaultdict
from typing import Any, Callable, Coroutine, Optional

import aiohttp

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None  # fall back to system default

log = logging.getLogger(__name__)

StateCallback = Callable[[str, dict], Coroutine]


class HAClient:
    """Async HA WebSocket client. Call `start()` once; it runs forever."""

    def __init__(self, ha_url: str, token: str) -> None:
        self._ha_url = ha_url.rstrip("/")
        self._token = token
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._sub_id: Optional[int] = None
        # entity_id → list of callbacks
        self._listeners: dict[str, list[StateCallback]] = defaultdict(list)
        self._wildcard_listeners: list[StateCallback] = []
        self._connected = asyncio.Event()
        self._running = False
        # Cache of entity states: entity_id → state dict
        self._state_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the connection loop. Run as a background task."""
        self._running = True
        connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT)
        self._session = aiohttp.ClientSession(connector=connector)
        backoff = 1
        while self._running:
            try:
                await self._connect()
                backoff = 1
            except Exception as exc:
                log.warning("HA WS disconnected: %s — retrying in %ds", exc, backoff)
                self._connected.clear()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def stop(self) -> None:
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()

    async def wait_connected(self, timeout: float = 30.0) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    def subscribe(self, entity_id: str, callback: StateCallback) -> None:
        """Register a callback for state changes of a specific entity."""
        self._listeners[entity_id].append(callback)

    def subscribe_all(self, callback: StateCallback) -> None:
        """Register a callback for ALL entity state changes."""
        self._wildcard_listeners.append(callback)

    def get_state(self, entity_id: str) -> Optional[dict]:
        """Return the cached state dict for an entity, or None."""
        return self._state_cache.get(entity_id)

    def get_state_attr(self, entity_id: str, attribute: str, default: Any = None) -> Any:
        state = self._state_cache.get(entity_id)
        if state is None:
            return default
        return state.get("attributes", {}).get(attribute, default)

    def get_numeric_state(self, entity_id: str) -> Optional[float]:
        """Return entity state as float, handling °C→°F conversion."""
        state = self._state_cache.get(entity_id)
        if state is None:
            return None
        try:
            value = float(state["state"])
        except (ValueError, KeyError, TypeError):
            return None
        unit = state.get("attributes", {}).get("unit_of_measurement", "")
        if unit == "°C":
            value = value * 9 / 5 + 32
        return value

    async def fetch_states(self) -> list[dict]:
        """Fetch all entity states via REST and populate cache."""
        ws_url = self._ha_url.replace("ws://", "http://").replace("wss://", "https://")
        async with self._session.get(
            f"{ws_url}/api/states",
            headers={"Authorization": f"Bearer {self._token}"},
        ) as resp:
            resp.raise_for_status()
            states = await resp.json()
        for s in states:
            self._state_cache[s["entity_id"]] = s
        return states

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: Optional[dict] = None,
    ) -> dict:
        """Call a HA service and await its response."""
        msg = {
            "type": "call_service",
            "domain": domain,
            "service": service,
            "service_data": service_data or {},
            "return_response": False,
        }
        return await self._send(msg)

    async def set_thermostat_temperature(self, entity_id: str, temperature: float) -> None:
        log.info("Setting %s setpoint → %.1f°F", entity_id, temperature)
        await self.call_service(
            "climate", "set_temperature",
            {"entity_id": entity_id, "temperature": temperature},
        )

    async def open_cover(self, entity_id: str) -> None:
        log.info("Opening vent %s", entity_id)
        await self.call_service("cover", "open_cover", {"entity_id": entity_id})

    async def close_cover(self, entity_id: str) -> None:
        log.info("Closing vent %s", entity_id)
        await self.call_service("cover", "close_cover", {"entity_id": entity_id})

    async def get_entities_by_domain(self, domain: str) -> list[dict]:
        """Return all cached states for a given domain."""
        await self._connected.wait()
        return [
            s for eid, s in self._state_cache.items()
            if eid.startswith(f"{domain}.")
        ]

    # ------------------------------------------------------------------
    # Internal connection lifecycle
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        ws_url = (
            self._ha_url
            .replace("http://", "ws://")
            .replace("https://", "wss://")
        ) + "/api/websocket"
        log.info("Connecting to %s", ws_url)
        async with self._session.ws_connect(ws_url) as ws:
            self._ws = ws
            await self._handshake()
            # Populate state cache before signalling ready
            try:
                await self.fetch_states()
            except Exception as exc:
                log.warning("Could not pre-fetch states: %s", exc)
            await self._subscribe_state_changed()
            self._connected.set()
            log.info("HA WebSocket connected and subscribed")
            await self._read_loop()

    async def _handshake(self) -> None:
        msg = await self._ws.receive_json()
        assert msg["type"] == "auth_required", f"Unexpected: {msg}"
        await self._ws.send_json({"type": "auth", "access_token": self._token})
        msg = await self._ws.receive_json()
        if msg["type"] != "auth_ok":
            raise ValueError(f"HA auth failed: {msg}")
        log.debug("HA auth OK (version=%s)", msg.get("ha_version"))

    async def _subscribe_state_changed(self) -> None:
        self._msg_id += 1
        sub_id = self._msg_id
        await self._ws.send_json({
            "id": sub_id,
            "type": "subscribe_events",
            "event_type": "state_changed",
        })
        resp = await self._ws.receive_json()
        assert resp.get("success"), f"Subscribe failed: {resp}"
        self._sub_id = sub_id

    async def _read_loop(self) -> None:
        async for msg in self._ws:
            if msg.type in (aiohttp.WSMsgType.TEXT,):
                data = json.loads(msg.data)
                await self._dispatch(data)
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                break

    async def _dispatch(self, data: dict) -> None:
        msg_type = data.get("type")
        if msg_type == "result":
            fut = self._pending.pop(data.get("id"), None)
            if fut and not fut.done():
                if data.get("success"):
                    fut.set_result(data.get("result", {}))
                else:
                    fut.set_exception(RuntimeError(str(data.get("error"))))
        elif msg_type == "event":
            event = data.get("event", {})
            if event.get("event_type") == "state_changed":
                ed = event.get("data", {})
                entity_id = ed.get("entity_id", "")
                new_state = ed.get("new_state") or {}
                if new_state:
                    self._state_cache[entity_id] = new_state
                # Fire entity-specific listeners
                for cb in self._listeners.get(entity_id, []):
                    asyncio.create_task(cb(entity_id, new_state))
                # Fire wildcard listeners
                for cb in self._wildcard_listeners:
                    asyncio.create_task(cb(entity_id, new_state))

    async def _send(self, payload: dict) -> dict:
        self._msg_id += 1
        payload["id"] = self._msg_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[self._msg_id] = fut
        await self._ws.send_json(payload)
        return await asyncio.wait_for(fut, timeout=10.0)


def build_ha_client() -> HAClient:
    ha_url = os.environ.get("HA_URL", "http://homeassistant.local:8123")
    token = os.environ.get("HA_TOKEN", "")
    # HA Supervisor: websocket endpoint is at ws://supervisor/core/websocket
    # but http API is at http://supervisor/core
    if ha_url.startswith("http://supervisor"):
        ws_url = ha_url.replace("http://supervisor/core", "ws://supervisor/core")
    else:
        ws_url = ha_url
    return HAClient(ws_url, token)
