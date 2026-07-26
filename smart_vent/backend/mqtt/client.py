"""The real broker connection, behind the transport the bridge expects (#519).

Kept deliberately thin: everything interesting is in :mod:`bridge`, which talks
to :class:`~backend.mqtt.bridge.MqttTransport` and never imports ``aiomqtt``.
That is what lets the whole topic tree, command dispatch, and discovery payloads
be tested without a broker — and what would let the client be swapped without
touching a line of the logic above it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from .config import MqttConfig
from .discovery import availability_topic

log = logging.getLogger(__name__)


class AiomqttTransport:
    """Adapts an ``aiomqtt.Client`` to the bridge's transport protocol."""

    def __init__(self, client) -> None:
        self._client = client

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        await self._client.publish(topic, payload=payload, retain=retain, qos=1)

    async def subscribe(self, topic: str) -> None:
        await self._client.subscribe(topic, qos=1)

    async def unsubscribe(self, topic: str) -> None:
        await self._client.unsubscribe(topic)

    async def messages(self) -> AsyncIterator[tuple[str, str, bool]]:
        async for message in self._client.messages:
            payload = message.payload
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode("utf-8", "replace")
            # The retain flag distinguishes a broker replay of a stored message
            # from a live publish — the bridge must never execute a replayed
            # command, so dropping the flag here would drop that safety.
            yield str(message.topic), str(payload), bool(message.retain)


class AiomqttConnection:
    """Async context manager yielding a connected :class:`AiomqttTransport`.

    The Last Will is registered at connect time so an ungraceful drop (power
    loss, container kill) flips every discovered entity to *unavailable* in HA
    instead of leaving stale retained values looking live.
    """

    def __init__(self, config: MqttConfig) -> None:
        self._config = config
        self._client: Any = None

    async def __aenter__(self) -> AiomqttTransport:
        import aiomqtt

        self._client = aiomqtt.Client(
            hostname=self._config.host,
            port=self._config.port,
            username=self._config.username,
            password=self._config.password,
            will=aiomqtt.Will(
                topic=availability_topic(self._config.prefix),
                payload="offline",
                retain=True,
                qos=1,
            ),
        )
        await self._client.__aenter__()
        return AiomqttTransport(self._client)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            # Say goodbye properly on a clean shutdown; the Will only covers the
            # ungraceful case.
            await client.publish(
                availability_topic(self._config.prefix), payload="offline", retain=True, qos=1
            )
        except Exception:  # noqa: BLE001 - never let cleanup mask the real error
            log.debug("Could not publish offline availability during shutdown", exc_info=True)
        await client.__aexit__(exc_type, exc, tb)


def connection_factory(config: MqttConfig):
    """Return a zero-arg factory producing a fresh connection per attempt."""
    return lambda: AiomqttConnection(config)
