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
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from .units import to_f

_SSL_CONTEXT: ssl.SSLContext | None
try:
    import certifi

    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None  # fall back to system default

log = logging.getLogger(__name__)

StateCallback = Callable[[str, dict], Coroutine]


class HAClient:
    """Async HA WebSocket client. Call `start()` once; it runs forever."""

    def __init__(self, ha_url: str, token: str, ssl_verify: bool = True) -> None:
        self._ha_url = ha_url.rstrip("/")
        self._token = token
        self._ssl_verify = ssl_verify
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._sub_id: int | None = None
        # entity_id → list of callbacks
        self._listeners: dict[str, list[StateCallback]] = defaultdict(list)
        self._wildcard_listeners: list[StateCallback] = []
        self._connected = asyncio.Event()
        self._running = False
        # Cache of entity states: entity_id → state dict
        self._state_cache: dict[str, dict] = {}
        # HA's configured system temperature unit ("F"/"C"). Climate entities
        # report and accept temperatures in this unit (unlike sensors, which
        # carry a per-entity unit_of_measurement). Resolved from /api/config on
        # connect and refreshed by get_temperature_unit(); defaults to "F" so
        # imperial installs and pre-connect reads behave unchanged. (Issue #280)
        self.ha_temp_unit: str = "F"
        # Developer mode: intercept all HA writes and log instead
        self.dev_mode: bool = False
        self._dev_logger: Any | None = None  # EventLogger, set externally

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the connection loop. Run as a background task."""
        self._running = True
        ssl_ctx: ssl.SSLContext | bool = (_SSL_CONTEXT or True) if self._ssl_verify else False
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
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

    def get_state(self, entity_id: str) -> dict | None:
        """Return the cached state dict for an entity, or None."""
        return self._state_cache.get(entity_id)

    def get_state_attr(self, entity_id: str, attribute: str, default: Any = None) -> Any:
        state = self._state_cache.get(entity_id)
        if state is None:
            return default
        return state.get("attributes", {}).get(attribute, default)

    def get_numeric_state(self, entity_id: str, max_age_min: float | None = None) -> float | None:
        """Return entity state as float, handling °C→°F conversion.

        If *max_age_min* is provided, ``None`` is returned for cached readings
        whose ``last_updated`` is more than that many minutes in the past — the
        freshness guard for control-loop reads (Issue #211). Callers that omit
        the argument see no behaviour change.
        """
        state = self._state_cache.get(entity_id)
        if state is None:
            return None
        try:
            value = float(state["state"])
        except (ValueError, KeyError, TypeError):
            return None
        if max_age_min is not None and self._state_age(state) > timedelta(minutes=max_age_min):
            return None
        unit = state.get("attributes", {}).get("unit_of_measurement", "")
        if unit == "°C":
            # Normalise to °F through the shared converter (Issue #251).
            value = to_f(value, "C")
        return value

    def get_state_age_seconds(self, entity_id: str) -> float | None:
        """Seconds since the entity last reported, or ``None`` if not cached.

        Used to surface a sensor's age in warning event logs (Issue #211).
        A missing or unparseable ``last_updated`` is reported as ``+inf`` so the
        caller treats it as stale rather than silently dropping the signal.
        """
        state = self._state_cache.get(entity_id)
        if state is None:
            return None
        return self._state_age(state).total_seconds()

    @staticmethod
    def _state_age(state: dict) -> timedelta:
        """Age of a cached state. Defaults to ``+inf`` on missing/bad timestamps."""
        raw = state.get("last_updated") or state.get("last_changed")
        if not raw:
            return timedelta.max
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return timedelta.max
        return datetime.now(UTC) - ts

    async def fetch_states(self) -> list[dict]:
        """Fetch all entity states via REST and populate cache."""
        assert self._session is not None
        ws_url = self._ha_url.replace("ws://", "http://").replace("wss://", "https://")
        async with self._session.get(
            f"{ws_url}/api/states",
            headers={"Authorization": f"Bearer {self._token}"},
        ) as resp:
            resp.raise_for_status()
            states: list[dict] = await resp.json()
        for s in states:
            self._state_cache[s["entity_id"]] = s
        return states

    async def get_temperature_unit(self) -> str:
        """Return 'F' or 'C' based on HA's configured unit system.

        Reads ``/api/config`` from HA. HA returns ``unit_system`` as an
        **object** — e.g. ``{"length": "km", "temperature": "°C", ...}`` — never
        the bare string ``"metric"``/``"imperial"``. The temperature unit is read
        from its ``temperature`` member: ``"°C"`` → ``"C"``, anything else
        (including a missing value or a legacy string shape) → ``"F"``.

        The resolved unit is cached on :attr:`ha_temp_unit` so the climate
        read/write path can normalise temperatures without a per-call REST hit.
        (Issue #281 — the previous ``== "metric"`` check never matched the object
        shape, so auto-detect could never return Celsius.)
        """
        assert self._session is not None
        ws_url = self._ha_url.replace("ws://", "http://").replace("wss://", "https://")
        async with self._session.get(
            f"{ws_url}/api/config",
            headers={"Authorization": f"Bearer {self._token}"},
        ) as resp:
            resp.raise_for_status()
            cfg = await resp.json()
        unit_system = cfg.get("unit_system") or {}
        temp = unit_system.get("temperature") if isinstance(unit_system, dict) else None
        unit = "C" if temp == "°C" else "F"
        self.ha_temp_unit = unit
        return unit

    def _setpoint_to_native(self, value_f: float) -> float:
        """Convert a stored °F setpoint to the climate entity's native unit.

        Climate entities accept ``temperature`` in HA's configured system unit.
        On a metric install that is °C, so a stored °F setpoint must be converted
        back before it is sent to ``climate.set_temperature`` — otherwise HA
        interprets the raw °F number as °C and drives the HVAC to a wildly wrong
        target. Identity for °F installs. (Issue #280)
        """
        if self.ha_temp_unit == "C":
            return round((value_f - 32) * 5 / 9, 2)
        return value_f

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict | None = None,
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

    async def set_thermostat_temperature(
        self,
        entity_id: str,
        temperature: float,
        hvac_mode: str | None = None,
    ) -> None:
        # Stored setpoints are °F; convert to the climate entity's native unit
        # (°C on a metric HA) so HA does not misread the number. (Issue #280)
        service_data: dict = {
            "entity_id": entity_id,
            "temperature": self._setpoint_to_native(temperature),
        }
        if hvac_mode is not None:
            service_data["hvac_mode"] = hvac_mode
        if self.dev_mode:
            mode_note = f", hvac_mode={hvac_mode}" if hvac_mode else ""
            log.info(
                "[DEV] Would set thermostat %s → %.1f°F%s",
                entity_id,
                temperature,
                mode_note,
            )
            if self._dev_logger:
                await self._dev_logger.log(
                    "info",
                    "dev",
                    f"Would set thermostat → {temperature:.1f}°F{mode_note}",
                    {
                        "entity_id": entity_id,
                        "temperature": temperature,
                        "hvac_mode": hvac_mode,
                        "action": "set_thermostat",
                    },
                )
            return
        log.info(
            "Setting %s setpoint → %.1f°F%s",
            entity_id,
            temperature,
            f", hvac_mode={hvac_mode}" if hvac_mode else "",
        )
        await self.call_service("climate", "set_temperature", service_data)

    async def set_thermostat_hvac_mode(self, entity_id: str, mode: str) -> None:
        if self.dev_mode:
            log.info("[DEV] Would set %s hvac_mode → %s", entity_id, mode)
            if self._dev_logger:
                await self._dev_logger.log(
                    "info",
                    "dev",
                    f"Would set thermostat {entity_id} hvac_mode → {mode}",
                    {"entity_id": entity_id, "hvac_mode": mode, "action": "set_hvac_mode"},
                )
            return
        log.info("Setting %s hvac_mode → %s", entity_id, mode)
        await self.call_service(
            "climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": mode}
        )

    async def set_thermostat_temperature_range(
        self, entity_id: str, low: float, high: float
    ) -> None:
        if self.dev_mode:
            log.info("[DEV] Would set %s range → %.1f–%.1f°F", entity_id, low, high)
            if self._dev_logger:
                await self._dev_logger.log(
                    "info",
                    "dev",
                    f"Would set thermostat {entity_id} range → {low:.1f}–{high:.1f}°F",
                    {
                        "entity_id": entity_id,
                        "target_temp_low": low,
                        "target_temp_high": high,
                        "action": "set_temperature_range",
                    },
                )
            return
        log.info("Setting %s range → %.1f–%.1f°F", entity_id, low, high)
        # Stored bounds are °F; convert to the entity's native unit. (Issue #280)
        await self.call_service(
            "climate",
            "set_temperature",
            {
                "entity_id": entity_id,
                "hvac_mode": "heat_cool",
                "target_temp_low": self._setpoint_to_native(low),
                "target_temp_high": self._setpoint_to_native(high),
            },
        )

    async def open_cover(self, entity_id: str) -> None:
        if self.dev_mode:
            msg = f"[DEV] Would open vent {entity_id}"
            log.info(msg)
            if self._dev_logger:
                await self._dev_logger.log(
                    "info",
                    "dev",
                    f"Would open vent {entity_id}",
                    {"entity_id": entity_id, "action": "open_vent"},
                )
            return
        log.info("Opening vent %s", entity_id)
        await self.call_service("cover", "open_cover", {"entity_id": entity_id})

    async def close_cover(self, entity_id: str) -> None:
        if self.dev_mode:
            msg = f"[DEV] Would close vent {entity_id}"
            log.info(msg)
            if self._dev_logger:
                await self._dev_logger.log(
                    "info",
                    "dev",
                    f"Would close vent {entity_id}",
                    {"entity_id": entity_id, "action": "close_vent"},
                )
            return
        log.info("Closing vent %s", entity_id)
        await self.call_service("cover", "close_cover", {"entity_id": entity_id})

    async def set_cover_position(self, entity_id: str, position: int) -> None:
        """cover.set_cover_position (0..100). Dev mode is a no-op logged to dev_logger."""
        if self.dev_mode:
            log.info("[DEV] Would set %s position → %d", entity_id, position)
            if self._dev_logger:
                await self._dev_logger.log(
                    "info",
                    "dev",
                    f"Would set vent {entity_id} position → {position}",
                    {"entity_id": entity_id, "action": "set_position", "position": position},
                )
            return
        log.info("Setting vent %s position → %d", entity_id, position)
        await self.call_service(
            "cover", "set_cover_position", {"entity_id": entity_id, "position": position}
        )

    async def set_cover_tilt_position(self, entity_id: str, tilt_position: int) -> None:
        """cover.set_cover_tilt_position (0..100). Used by integrations (e.g. some
        Flair vents) that expose tilt rather than open/close or position."""
        if self.dev_mode:
            log.info("[DEV] Would set %s tilt → %d", entity_id, tilt_position)
            if self._dev_logger:
                await self._dev_logger.log(
                    "info",
                    "dev",
                    f"Would set vent {entity_id} tilt → {tilt_position}",
                    {
                        "entity_id": entity_id,
                        "action": "set_tilt_position",
                        "tilt_position": tilt_position,
                    },
                )
            return
        log.info("Setting vent %s tilt → %d", entity_id, tilt_position)
        await self.call_service(
            "cover",
            "set_cover_tilt_position",
            {"entity_id": entity_id, "tilt_position": tilt_position},
        )

    async def toggle_cover(self, entity_id: str) -> None:
        """cover.toggle — invert current state. Caller must ensure the current
        state does not already match the desired state (vent_controller does
        this via its state-match skip)."""
        if self.dev_mode:
            log.info("[DEV] Would toggle %s", entity_id)
            if self._dev_logger:
                await self._dev_logger.log(
                    "info",
                    "dev",
                    f"Would toggle vent {entity_id}",
                    {"entity_id": entity_id, "action": "toggle"},
                )
            return
        log.info("Toggling vent %s", entity_id)
        await self.call_service("cover", "toggle", {"entity_id": entity_id})

    async def get_entities_by_domain(self, domain: str) -> list[dict]:
        """Return all cached states for a given domain."""
        await self._connected.wait()
        return [s for eid, s in self._state_cache.items() if eid.startswith(f"{domain}.")]

    # ------------------------------------------------------------------
    # Internal connection lifecycle
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        assert self._session is not None
        ws_url = (
            self._ha_url.replace("http://", "ws://").replace("https://", "wss://")
        ) + "/api/websocket"
        log.info("Connecting to %s", ws_url)
        try:
            async with self._session.ws_connect(ws_url) as ws:
                self._ws = ws
                await self._handshake()
                # Populate state cache before signalling ready
                try:
                    await self.fetch_states()
                except Exception as exc:
                    log.warning("Could not pre-fetch states: %s", exc)
                # Resolve HA's system temperature unit so the climate read/write
                # path can normalise to °F, independent of the display-unit
                # override that may pin the UI to a different unit. (Issue #280)
                try:
                    await self.get_temperature_unit()
                except Exception as exc:
                    log.debug("Could not resolve HA temperature unit on connect: %s", exc)
                await self._subscribe_state_changed()
                self._connected.set()
                log.info("HA WebSocket connected and subscribed")
                await self._read_loop()
        finally:
            # Always clean up so stale _ws is never used and in-flight callers
            # fail immediately instead of waiting for their per-call timeout.
            self._ws = None
            self._connected.clear()
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError("HA WebSocket disconnected"))
            self._pending.clear()

    async def _handshake(self) -> None:
        assert self._ws is not None
        msg = await self._ws.receive_json()
        assert msg["type"] == "auth_required", f"Unexpected: {msg}"
        await self._ws.send_json({"type": "auth", "access_token": self._token})
        msg = await self._ws.receive_json()
        if msg["type"] != "auth_ok":
            raise ValueError(f"HA auth failed: {msg}")
        log.debug("HA auth OK (version=%s)", msg.get("ha_version"))

    async def _subscribe_state_changed(self) -> None:
        assert self._ws is not None
        self._msg_id += 1
        sub_id = self._msg_id
        await self._ws.send_json(
            {
                "id": sub_id,
                "type": "subscribe_events",
                "event_type": "state_changed",
            }
        )
        resp = await self._ws.receive_json()
        assert resp.get("success"), f"Subscribe failed: {resp}"
        self._sub_id = sub_id

    async def _read_loop(self) -> None:
        assert self._ws is not None
        async for msg in self._ws:
            if msg.type in (aiohttp.WSMsgType.TEXT,):
                data = json.loads(msg.data)
                await self._dispatch(data)
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                break

    async def _dispatch(self, data: dict) -> None:
        msg_type = data.get("type")
        if msg_type == "result":
            msg_id: int | None = data.get("id")
            fut = self._pending.pop(msg_id, None) if msg_id is not None else None
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
        if not self._connected.is_set():
            raise RuntimeError("HA not connected")
        assert self._ws is not None
        self._msg_id += 1
        msg_id = self._msg_id
        payload["id"] = msg_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await asyncio.wait_for(self._ws.send_json(payload), timeout=5.0)
        except Exception:
            self._pending.pop(msg_id, None)
            if not fut.done():
                fut.cancel()
            raise
        return await asyncio.wait_for(fut, timeout=10.0)


def build_ha_client() -> HAClient:
    # ------------------------------------------------------------------
    # Read all possible token/URL sources and log each one so we can
    # diagnose supervisor injection issues from the add-on logs.
    # ------------------------------------------------------------------
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN") or ""
    ha_token_env = os.environ.get("HA_TOKEN") or ""
    ha_url_env = os.environ.get("HA_URL") or ""

    log.info(
        "Token sources — SUPERVISOR_TOKEN(python): %s | HA_TOKEN(env): %s",
        "present" if supervisor_token else "NOT FOUND",
        "present" if ha_token_env else "not set",
    )
    log.info("HA_URL from environment: %r", ha_url_env or "(empty)")

    # ------------------------------------------------------------------
    # Resolve final URL and token, with fallback chain:
    #   1. HA_URL/HA_TOKEN set by run.sh (preferred — run.sh already
    #      applied the supervisor-proxy / user-config logic)
    #   2. Python-level SUPERVISOR_TOKEN fallback in case run.sh env
    #      injection failed but Python still sees the Docker env var
    # ------------------------------------------------------------------
    ha_url = ha_url_env
    token = ha_token_env
    url_source = "env"
    token_source = "env"

    if not ha_url:
        if supervisor_token:
            ha_url = "http://supervisor/core"
            url_source = "python-supervisor-fallback"
        else:
            ha_url = "http://homeassistant.local:8123"
            url_source = "hardcoded-default"

    if not token:
        if supervisor_token:
            token = supervisor_token
            token_source = "python-SUPERVISOR_TOKEN-fallback"
        else:
            token_source = "empty — no token available"

    log.info("Resolved — URL: %s (source: %s)", ha_url, url_source)
    log.info(
        "Resolved — token: %s (source: %s)",
        "present" if token else "MISSING",
        token_source,
    )

    use_wss = os.environ.get("HA_USE_WSS", "false").lower() in ("1", "true", "yes")
    ssl_verify = os.environ.get("HA_SSL_VERIFY", "true").lower() not in (
        "0",
        "false",
        "no",
    )

    log.info("Options — use_wss: %s | ssl_verify: %s", use_wss, ssl_verify)

    # ------------------------------------------------------------------
    # Build WebSocket URL
    # ------------------------------------------------------------------
    if ha_url.startswith("http://supervisor"):
        # Internal supervisor proxy — always plain ws://, never TLS
        ws_url = ha_url.replace("http://supervisor/core", "ws://supervisor/core")
    elif use_wss:
        # Force wss:// even if URL scheme is http://
        ws_url = ha_url.replace("http://", "https://").replace("ws://", "wss://")
    else:
        # Auto-detect: https → wss, http → ws
        ws_url = ha_url.replace("http://", "ws://").replace("https://", "wss://")

    log.info("WebSocket URL: %s | ssl_verify: %s", ws_url, ssl_verify)
    return HAClient(ws_url, token, ssl_verify=ssl_verify)
