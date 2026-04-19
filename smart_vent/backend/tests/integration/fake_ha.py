"""
In-memory stand-in for ``HAClient`` used by integration tests.

Mirrors every public method on ``smart_vent.backend.ha_client.HAClient`` so
integration tests don't silently rely on ``MagicMock`` attribute
auto-creation. The real HA client is a WebSocket client that caches
entity states and forwards service calls over the wire; this fake does the
same thing against a local dict, records every service call it receives,
and dispatches to registered subscribers when entity state changes.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

StateCallback = Callable[[str, dict], Coroutine]


@dataclass
class ServiceCall:
    domain: str
    service: str
    data: dict[str, Any]


class FakeHomeAssistant:
    """Concrete HAClient stand-in backed by an in-memory state dict."""

    def __init__(self) -> None:
        self._state: dict[str, dict] = {}
        self._listeners: dict[str, list[StateCallback]] = defaultdict(list)
        self._wildcard_listeners: list[StateCallback] = []
        self._connected = asyncio.Event()
        self._connected.set()
        self.calls: list[ServiceCall] = []
        self.dev_mode: bool = False
        self._dev_logger: Any | None = None

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def seed_state(self, entity_id: str, state: str, attributes: dict | None = None) -> None:
        """Set an entity's state without firing subscribers (setup)."""
        self._state[entity_id] = {
            "entity_id": entity_id,
            "state": state,
            "attributes": dict(attributes or {}),
        }

    async def set_entity_state(
        self, entity_id: str, state: str, attributes: dict | None = None
    ) -> None:
        """Mutate state and dispatch subscribers (simulates state_changed)."""
        self._state[entity_id] = {
            "entity_id": entity_id,
            "state": state,
            "attributes": dict(attributes or {}),
        }
        await self._dispatch(entity_id)

    def reset_calls(self) -> None:
        self.calls.clear()

    def calls_for(self, service: str) -> list[ServiceCall]:
        return [c for c in self.calls if c.service == service]

    # ------------------------------------------------------------------
    # Mirror of HAClient public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._connected.set()

    async def stop(self) -> None:
        self._connected.clear()

    async def wait_connected(self, timeout: float = 30.0) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    def subscribe(self, entity_id: str, callback: StateCallback) -> None:
        self._listeners[entity_id].append(callback)

    def subscribe_all(self, callback: StateCallback) -> None:
        self._wildcard_listeners.append(callback)

    def get_state(self, entity_id: str) -> dict | None:
        return self._state.get(entity_id)

    def get_state_attr(self, entity_id: str, attribute: str, default: Any = None) -> Any:
        s = self._state.get(entity_id)
        if s is None:
            return default
        return s.get("attributes", {}).get(attribute, default)

    def get_numeric_state(self, entity_id: str) -> float | None:
        s = self._state.get(entity_id)
        if s is None:
            return None
        try:
            value = float(s["state"])
        except (ValueError, KeyError, TypeError):
            return None
        unit = s.get("attributes", {}).get("unit_of_measurement", "")
        if unit == "°C":
            value = value * 9 / 5 + 32
        return value

    async def fetch_states(self) -> list[dict]:
        return list(self._state.values())

    async def call_service(
        self, domain: str, service: str, service_data: dict | None = None
    ) -> dict:
        self.calls.append(
            ServiceCall(domain=domain, service=service, data=dict(service_data or {}))
        )
        return {}

    async def set_thermostat_temperature(
        self,
        entity_id: str,
        temperature: float,
        hvac_mode: str | None = None,
    ) -> None:
        data: dict = {"entity_id": entity_id, "temperature": temperature}
        if hvac_mode is not None:
            data["hvac_mode"] = hvac_mode
        if self.dev_mode:
            await self._dev_log(
                f"Would set thermostat → {temperature:.1f}°F",
                {
                    "entity_id": entity_id,
                    "temperature": temperature,
                    "hvac_mode": hvac_mode,
                    "action": "set_thermostat",
                },
            )
            return
        self.calls.append(ServiceCall(domain="climate", service="set_temperature", data=data))
        # Silently reflect the write into state so subsequent reads see the
        # new setpoint. We do NOT call set_entity_state here — real HA
        # surfaces state_changed on a different event loop tick, and
        # dispatching subscribers synchronously inside the engine's own
        # write path would re-enter the engine lock. Tests that want to
        # simulate a downstream state_changed should use set_entity_state.
        cur = self._state.get(entity_id, {"entity_id": entity_id, "attributes": {}})
        attrs = dict(cur.get("attributes") or {})
        attrs["temperature"] = temperature
        self._state[entity_id] = {
            "entity_id": entity_id,
            "state": cur.get("state", "cool"),
            "attributes": attrs,
        }

    async def open_cover(self, entity_id: str) -> None:
        if self.dev_mode:
            await self._dev_log(
                f"Would open vent {entity_id}",
                {"entity_id": entity_id, "action": "open_vent"},
            )
            return
        self.calls.append(
            ServiceCall(domain="cover", service="open_cover", data={"entity_id": entity_id})
        )
        cur = self._state.get(entity_id, {"attributes": {}})
        self._state[entity_id] = {
            "entity_id": entity_id,
            "state": "open",
            "attributes": dict(cur.get("attributes") or {}),
        }

    async def close_cover(self, entity_id: str) -> None:
        if self.dev_mode:
            await self._dev_log(
                f"Would close vent {entity_id}",
                {"entity_id": entity_id, "action": "close_vent"},
            )
            return
        self.calls.append(
            ServiceCall(domain="cover", service="close_cover", data={"entity_id": entity_id})
        )
        cur = self._state.get(entity_id, {"attributes": {}})
        self._state[entity_id] = {
            "entity_id": entity_id,
            "state": "closed",
            "attributes": dict(cur.get("attributes") or {}),
        }

    async def set_cover_position(self, entity_id: str, position: int) -> None:
        if self.dev_mode:
            await self._dev_log(
                f"Would set vent {entity_id} position → {position}",
                {
                    "entity_id": entity_id,
                    "action": "set_position",
                    "position": position,
                },
            )
            return
        self.calls.append(
            ServiceCall(
                domain="cover",
                service="set_cover_position",
                data={"entity_id": entity_id, "position": position},
            )
        )
        cur = self._state.get(entity_id, {"attributes": {}})
        attrs = dict(cur.get("attributes") or {})
        attrs["current_position"] = position
        self._state[entity_id] = {
            "entity_id": entity_id,
            "state": "closed" if position == 0 else "open",
            "attributes": attrs,
        }

    async def set_cover_tilt_position(self, entity_id: str, tilt_position: int) -> None:
        if self.dev_mode:
            await self._dev_log(
                f"Would set vent {entity_id} tilt → {tilt_position}",
                {
                    "entity_id": entity_id,
                    "action": "set_tilt_position",
                    "tilt_position": tilt_position,
                },
            )
            return
        self.calls.append(
            ServiceCall(
                domain="cover",
                service="set_cover_tilt_position",
                data={"entity_id": entity_id, "tilt_position": tilt_position},
            )
        )
        cur = self._state.get(entity_id, {"attributes": {}})
        attrs = dict(cur.get("attributes") or {})
        attrs["current_tilt_position"] = tilt_position
        self._state[entity_id] = {
            "entity_id": entity_id,
            "state": "closed" if tilt_position == 0 else "open",
            "attributes": attrs,
        }

    async def toggle_cover(self, entity_id: str) -> None:
        if self.dev_mode:
            await self._dev_log(
                f"Would toggle vent {entity_id}",
                {"entity_id": entity_id, "action": "toggle"},
            )
            return
        self.calls.append(
            ServiceCall(domain="cover", service="toggle", data={"entity_id": entity_id})
        )
        cur = self._state.get(entity_id, {"state": "closed", "attributes": {}})
        new_state = "open" if cur.get("state") == "closed" else "closed"
        self._state[entity_id] = {
            "entity_id": entity_id,
            "state": new_state,
            "attributes": dict(cur.get("attributes") or {}),
        }

    async def get_entities_by_domain(self, domain: str) -> list[dict]:
        await self._connected.wait()
        return [s for eid, s in self._state.items() if eid.startswith(f"{domain}.")]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _dev_log(self, message: str, details: dict) -> None:
        if self._dev_logger:
            await self._dev_logger.log("info", "dev", message, details)

    async def _dispatch(self, entity_id: str) -> None:
        """Fire registered callbacks for an entity. Awaits each callback
        directly so integration tests get deterministic ordering without
        needing to drain ``asyncio.create_task`` futures."""
        new_state = self._state.get(entity_id) or {}
        for cb in list(self._listeners.get(entity_id, [])):
            await cb(entity_id, new_state)
        for cb in list(self._wildcard_listeners):
            await cb(entity_id, new_state)


@dataclass
class SeededEntities:
    """Ergonomic bundle of common entity IDs, seeded into a FakeHomeAssistant.

    Tests that want a typical single-thermostat setup can do::

        seeded = SeededEntities.default(fake_ha)

    and get back a dataclass with ``thermostat``, ``sensor``, ``vent``,
    ``presence`` entity IDs already populated in ``fake_ha`` state.
    """

    thermostat: str
    sensor: str
    vent: str
    presence: str

    @classmethod
    def default(
        cls,
        ha: FakeHomeAssistant,
        *,
        ambient_temp: float = 72.0,
        setpoint: float = 72.0,
        room_temp: float = 72.0,
        vent_state: str = "open",
        hvac_action: str = "idle",
    ) -> SeededEntities:
        thermo = "climate.test_thermostat"
        sensor = "sensor.test_room_temp"
        vent = "cover.test_room_vent"
        presence = "binary_sensor.test_room_presence"
        ha.seed_state(
            thermo,
            "cool",
            {
                "current_temperature": ambient_temp,
                "temperature": setpoint,
                "hvac_action": hvac_action,
            },
        )
        ha.seed_state(sensor, str(room_temp), {"unit_of_measurement": "°F"})
        ha.seed_state(vent, vent_state, {})
        ha.seed_state(presence, "off", {})
        return cls(thermostat=thermo, sensor=sensor, vent=vent, presence=presence)

    __test__ = False  # not a test class — avoid pytest collection
