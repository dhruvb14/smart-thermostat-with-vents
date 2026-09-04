"""The aiomqtt adapter (Issue #519).

``bridge.py`` never imports ``aiomqtt``; this module is the only place that
does, so these tests stub the library out and check the adaptation itself —
payload decoding, QoS, and above all the availability handling that decides
whether HA shows Plenum's entities as live or unavailable.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from backend.mqtt.client import AiomqttConnection, AiomqttTransport, connection_factory
from backend.mqtt.config import MqttConfig

PREFIX = "plenum"


def _config(**overrides) -> MqttConfig:
    base: dict[str, Any] = {
        "host": "broker.local",
        "port": 1883,
        "username": "user",
        "password": "pw",
        "prefix": PREFIX,
        "discovery": True,
        "discovery_prefix": "homeassistant",
    }
    base.update(overrides)
    return MqttConfig(**base)


class FakeMessage:
    def __init__(self, topic: str, payload, retain: bool = False) -> None:
        self.topic = topic
        self.payload = payload
        self.retain = retain


class FakeAiomqttClient:
    """Stands in for ``aiomqtt.Client``."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.published: list[dict] = []
        self.subscribed: list[tuple[str, int]] = []
        self.unsubscribed: list[str] = []
        self.entered = False
        self.exited = False
        self.messages = self._messages()

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc) -> None:
        self.exited = True

    async def publish(self, topic, payload=None, retain=False, qos=0) -> None:
        self.published.append({"topic": topic, "payload": payload, "retain": retain, "qos": qos})

    async def subscribe(self, topic, qos=0) -> None:
        self.subscribed.append((topic, qos))

    async def unsubscribe(self, topic) -> None:
        self.unsubscribed.append(topic)

    async def _messages(self):  # pragma: no cover - replaced per test
        return
        yield


class FakeWill:
    def __init__(self, topic, payload, retain=False, qos=0) -> None:
        self.topic = topic
        self.payload = payload
        self.retain = retain
        self.qos = qos


@pytest.fixture
def fake_aiomqtt(monkeypatch: pytest.MonkeyPatch):
    """Install a stub ``aiomqtt`` module for the duration of a test."""
    created: list[FakeAiomqttClient] = []

    def _client(**kwargs):
        client = FakeAiomqttClient(**kwargs)
        created.append(client)
        return client

    module = types.ModuleType("aiomqtt")
    module.Client = _client  # type: ignore[attr-defined]
    module.Will = FakeWill  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aiomqtt", module)
    return created


class TestTransport:
    @pytest.mark.asyncio
    async def test_publish_uses_qos_1(self) -> None:
        """Config changes must not be silently dropped by fire-and-forget QoS 0."""
        client = FakeAiomqttClient()
        await AiomqttTransport(client).publish("t", "payload", True)
        assert client.published == [{"topic": "t", "payload": "payload", "retain": True, "qos": 1}]

    @pytest.mark.asyncio
    async def test_subscribe_uses_qos_1(self) -> None:
        client = FakeAiomqttClient()
        await AiomqttTransport(client).subscribe("plenum/room/#")
        assert client.subscribed == [("plenum/room/#", 1)]

    @pytest.mark.asyncio
    async def test_unsubscribe_passes_through(self) -> None:
        client = FakeAiomqttClient()
        await AiomqttTransport(client).unsubscribe("homeassistant/+/+/config")
        assert client.unsubscribed == ["homeassistant/+/+/config"]

    @pytest.mark.asyncio
    async def test_messages_decode_bytes_to_text(self) -> None:
        client = FakeAiomqttClient()

        async def _messages():
            yield FakeMessage("plenum/room/a/hold/set", b"72")
            yield FakeMessage("plenum/room/a/hold/set", "73")

        client.messages = _messages()
        got = [m async for m in AiomqttTransport(client).messages()]
        assert got == [
            ("plenum/room/a/hold/set", "72", False),
            ("plenum/room/a/hold/set", "73", False),
        ]

    @pytest.mark.asyncio
    async def test_messages_carry_the_retain_flag(self) -> None:
        """The bridge refuses to execute broker replays of retained commands —
        which is only possible if the flag survives the transport."""
        client = FakeAiomqttClient()

        async def _messages():
            yield FakeMessage("plenum/room/a/hold/set", "72", retain=True)

        client.messages = _messages()
        got = [m async for m in AiomqttTransport(client).messages()]
        assert got == [("plenum/room/a/hold/set", "72", True)]

    @pytest.mark.asyncio
    async def test_undecodable_payload_does_not_raise(self) -> None:
        """A publisher can put arbitrary bytes on our topics; the read loop must
        survive them and let the command parser reject the value instead."""
        client = FakeAiomqttClient()

        async def _messages():
            yield FakeMessage("plenum/room/a/hold/set", b"\xff\xfe")

        client.messages = _messages()
        got = [m async for m in AiomqttTransport(client).messages()]
        assert len(got) == 1 and isinstance(got[0][1], str)


class TestConnection:
    @pytest.mark.asyncio
    async def test_passes_the_broker_settings_through(self, fake_aiomqtt) -> None:
        async with AiomqttConnection(_config()):
            pass
        kwargs = fake_aiomqtt[0].kwargs
        assert kwargs["hostname"] == "broker.local"
        assert kwargs["port"] == 1883
        assert kwargs["username"] == "user"
        assert kwargs["password"] == "pw"

    @pytest.mark.asyncio
    async def test_registers_a_retained_offline_will(self, fake_aiomqtt) -> None:
        """Without the LWT, a killed container leaves HA showing stale retained
        values as if they were live."""
        async with AiomqttConnection(_config()):
            pass
        will = fake_aiomqtt[0].kwargs["will"]
        assert will.topic == f"{PREFIX}/status"
        assert will.payload == "offline"
        assert will.retain is True

    @pytest.mark.asyncio
    async def test_says_goodbye_on_a_clean_shutdown(self, fake_aiomqtt) -> None:
        """The Will only covers ungraceful drops."""
        async with AiomqttConnection(_config()):
            pass
        client = fake_aiomqtt[0]
        assert client.exited is True
        assert {"topic": f"{PREFIX}/status", "payload": "offline", "retain": True, "qos": 1} in (
            client.published
        )

    @pytest.mark.asyncio
    async def test_a_failing_goodbye_does_not_mask_the_real_error(
        self, fake_aiomqtt, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = AiomqttConnection(_config())
        await connection.__aenter__()

        async def _boom(*a, **kw):
            raise OSError("socket already gone")

        monkeypatch.setattr(connection._client, "publish", _boom)
        # Must complete rather than raising over whatever ended the session.
        await connection.__aexit__(None, None, None)
        # ...and must still close the client. Swallowing the goodbye failure by
        # returning early would leak the socket on every shutdown.
        assert fake_aiomqtt[0].exited is True

    @pytest.mark.asyncio
    async def test_exit_without_enter_is_a_no_op(self) -> None:
        await AiomqttConnection(_config()).__aexit__(None, None, None)

    def test_factory_produces_a_fresh_connection_each_time(self) -> None:
        """A reconnect must not reuse a dead client object."""
        factory = connection_factory(_config())
        assert factory() is not factory()
