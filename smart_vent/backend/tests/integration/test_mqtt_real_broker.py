"""The MQTT bridge against a REAL broker (Issue #519).

Everything else in the MQTT suite fakes the transport, which is the right trade
for testing logic — but it cannot show that the wire format is right, that
retained messages behave the way Home Assistant depends on, or that ``aiomqtt``
is driven correctly. This module runs an in-process ``amqtt`` broker and puts
the actual ``aiomqtt`` client on it, so the whole path is exercised:

    published message → broker → aiomqtt → bridge → loopback REST → SQLite

The docker-compose ``mosquitto`` leg in CI covers the same ground against the
broker people really run; this exists so the check is also part of the ordinary
``pytest`` run, where it catches a regression in seconds instead of after a
container build.

Skipped when ``amqtt`` is missing, so a slim environment still runs the suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("amqtt", reason="in-process MQTT broker not installed")

import aiohttp  # noqa: E402
import aiomqtt  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from amqtt.broker import Broker  # noqa: E402

from backend import db as _db  # noqa: E402
from backend.main import build_app, build_loopback_dispatch  # noqa: E402
from backend.mqtt.bridge import MqttBridge  # noqa: E402
from backend.mqtt.client import connection_factory  # noqa: E402
from backend.mqtt.config import MqttConfig  # noqa: E402

from .fake_ha import FakeHomeAssistant  # noqa: E402

PREFIX = "plenum"
THERMO = "climate.test_thermostat"
# Bounds every wait; a real broker makes everything asynchronous, so nothing
# here may spin forever if a message never arrives.
DEADLINE = 20.0


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Listener:
    """An independent subscriber — what Home Assistant is, from our side."""

    def __init__(self) -> None:
        self.latest: dict[str, str] = {}
        self.arrivals: list[tuple[str, str]] = []

    def count(self, topic: str) -> int:
        return sum(1 for t, _ in self.arrivals if t == topic)

    async def run(self, port: int) -> None:
        async with aiomqtt.Client("127.0.0.1", port) as sub:
            await sub.subscribe("#", qos=1)
            async for message in sub.messages:
                payload = message.payload.decode("utf-8", "replace")
                self.latest[str(message.topic)] = payload
                self.arrivals.append((str(message.topic), payload))

    async def wait(self, predicate, what: str) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + DEADLINE
        while loop.time() < end:
            if predicate():
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out after {DEADLINE}s waiting for {what}")

    async def wait_topic(self, topic: str) -> str:
        await self.wait(lambda: topic in self.latest, f"a message on {topic}")
        return self.latest[topic]

    async def wait_new(self, topic: str, since: int) -> str:
        """Wait for a *new* message on a topic that already has one.

        Result topics are reused, so "did my command get a verdict?" cannot be
        answered from the latest value alone.
        """
        await self.wait(lambda: self.count(topic) > since, f"a new message on {topic}")
        return self.latest[topic]


@pytest.fixture
async def stack() -> AsyncIterator[dict]:
    """A real broker + the real app + a connected bridge + a subscriber."""
    port = _free_port()
    broker = Broker(
        {
            "listeners": {
                "default": {"type": "tcp", "bind": f"127.0.0.1:{port}", "max_connections": 50}
            },
            "sys_interval": 0,
            "auth": {"allow-anonymous": True, "plugins": ["auth_anonymous"]},
            "topic-check": {"enabled": False},
        }
    )
    await broker.start()

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = build_app(
        FakeHomeAssistant(),  # type: ignore[arg-type]
        db_path,
        frontend_dist=None,
        start_ha=False,
    )
    client = TestClient(TestServer(app))
    await client.start_server()

    session = aiohttp.ClientSession()
    config = MqttConfig(
        enabled=True,
        host="127.0.0.1",
        port=port,
        username=None,
        password=None,
        prefix=PREFIX,
        discovery=True,
        discovery_prefix="homeassistant",
    )
    bridge = MqttBridge(
        config,
        build_loopback_dispatch(
            session, f"http://127.0.0.1:{client.server.port}", app["internal_token"]
        ),
        connection_factory(config),
        refresh_seconds=0.5,
    )
    bridge_task = asyncio.create_task(bridge.run())

    listener = Listener()
    listen_task = asyncio.create_task(listener.run(port))

    await listener.wait(
        lambda: listener.latest.get(f"{PREFIX}/status") == "online",
        "the bridge to announce itself online",
    )

    async def publish(topic: str, payload: str) -> None:
        async with aiomqtt.Client("127.0.0.1", port) as pub:
            await pub.publish(topic, payload=payload, qos=1)

    try:
        yield {
            "client": client,
            "app": app,
            "bridge": bridge,
            "listener": listener,
            "publish": publish,
            "port": port,
        }
    finally:
        # Order matters. Drop our clients first and let the broker notice, then
        # shut the broker down: amqtt's shutdown tries to flush QoS-1 traffic to
        # still-registered sessions and raises CancelledError waiting for a
        # PUBACK that a torn-down client will never send. That is an amqtt
        # shutdown race, not a Plenum failure, so it must not fail the test —
        # hence the suppression around the broker itself.
        bridge.stop()
        for task in (bridge_task, listen_task):
            task.cancel()
        for task in (bridge_task, listen_task):
            with contextlib.suppress(BaseException):
                await task
        await asyncio.sleep(0.1)
        await session.close()
        await client.close()
        # Bounded: with its clients gone but their sessions still registered,
        # amqtt's shutdown can sit waiting on QoS-1 acknowledgements that will
        # never arrive. The broker is about to be discarded either way, so cap
        # the wait rather than letting a teardown detail hang the suite.
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(broker.shutdown(), timeout=5)
        os.unlink(db_path)


async def _make_room(client, name="Mqtt Test Room") -> str:
    resp = await client.post("/api/rooms", json={"name": name, "thermostat_entity_id": THERMO})
    assert resp.status == 201, await resp.text()
    return str((await resp.json())["id"])


@pytest.mark.asyncio
async def test_availability_is_announced_online(stack) -> None:
    """The fixture already waited for this; assert it explicitly so a failure
    here reads as "the bridge never came up" rather than a timeout elsewhere."""
    assert stack["listener"].latest[f"{PREFIX}/status"] == "online"


@pytest.mark.asyncio
async def test_command_travels_broker_to_database(stack) -> None:
    client, listener = stack["client"], stack["listener"]
    room_id = await _make_room(client)
    stack["bridge"].request_sync()
    await listener.wait_topic(f"{PREFIX}/room/{room_id}/temp_offset/state")

    await stack["publish"](f"{PREFIX}/room/{room_id}/temp_offset/set", "1.5")
    result = await listener.wait_topic(f"{PREFIX}/room/{room_id}/temp_offset/set/result")

    assert json.loads(result) == {"ok": True}
    room = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert room["temp_offset"] == 1.5


@pytest.mark.asyncio
async def test_state_is_republished_after_a_command(stack) -> None:
    client, listener = stack["client"], stack["listener"]
    room_id = await _make_room(client)
    state = f"{PREFIX}/room/{room_id}/temp_offset/state"
    await listener.wait_topic(state)

    await stack["publish"](f"{PREFIX}/room/{room_id}/temp_offset/set", "3")
    await listener.wait(lambda: listener.latest.get(state) == "3", "the state to update")


@pytest.mark.asyncio
async def test_addressing_by_room_name_works_over_the_wire(stack) -> None:
    client, listener = stack["client"], stack["listener"]
    room_id = await _make_room(client, name="Living Room")
    await listener.wait_topic(f"{PREFIX}/room/living_room/temp_offset/state")

    await stack["publish"](f"{PREFIX}/room/living_room/temp_offset/set", "2.5")
    result = await listener.wait_topic(f"{PREFIX}/room/living_room/temp_offset/set/result")

    assert json.loads(result) == {"ok": True}
    room = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert room["temp_offset"] == 2.5


@pytest.mark.asyncio
async def test_a_rejected_command_reports_and_changes_nothing(stack) -> None:
    """#519's driving requirement — a rejected write must never look like it
    worked, and must not have half-applied."""
    client, listener = stack["client"], stack["listener"]
    room_id = await _make_room(client)
    await listener.wait_topic(f"{PREFIX}/room/{room_id}/temp_offset/state")

    await stack["publish"](f"{PREFIX}/room/{room_id}/system_wide_temp/set", "999")
    result = await listener.wait_topic(f"{PREFIX}/room/{room_id}/system_wide_temp/set/result")

    parsed = json.loads(result)
    assert parsed["ok"] is False
    assert "system_wide_temp" in parsed["error"]
    room = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert room["system_wide_temp"] is None


@pytest.mark.asyncio
async def test_a_fresh_subscriber_receives_retained_state(stack) -> None:
    """How HA sees the world after a restart: it subscribes and the broker
    replays retained values. If state were published unretained, every HA
    restart would show every Plenum entity as unknown."""
    client = stack["client"]
    room_id = await _make_room(client)
    stack["bridge"].request_sync()
    state = f"{PREFIX}/room/{room_id}/temp_offset/state"
    await stack["listener"].wait_topic(state)

    seen: dict[str, str] = {}

    async def collect() -> None:
        async with aiomqtt.Client("127.0.0.1", stack["port"]) as fresh:
            await fresh.subscribe(f"{PREFIX}/#", qos=1)
            async for message in fresh.messages:
                seen[str(message.topic)] = message.payload.decode()

    task = asyncio.create_task(collect())
    try:
        await stack["listener"].wait(
            lambda: state in seen and f"{PREFIX}/status" in seen,
            "the broker to replay retained state to a brand-new subscriber",
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    assert seen[state] is not None, "retained state was not replayed to a new subscriber"
    assert seen[f"{PREFIX}/status"] == "online"


@pytest.mark.asyncio
async def test_discovery_configs_reach_the_broker_id_addressed(stack) -> None:
    client, listener = stack["client"], stack["listener"]
    room_id = await _make_room(client, name="Living Room")
    stack["bridge"].request_sync()
    await listener.wait(
        lambda: any(t.startswith("homeassistant/") and room_id in t for t in listener.latest),
        "HA discovery configs for the new room",
    )

    configs = {
        t: json.loads(p) for t, p in listener.latest.items() if t.startswith("homeassistant/") and p
    }
    assert configs
    assert any(
        c.get("command_topic", "").endswith(f"/room/{room_id}/hold/set") for c in configs.values()
    )
    # Renaming must never disturb HA's registry, so nothing may key off the name.
    assert all("/room/living_room/" not in json.dumps(c) for c in configs.values())


@pytest.mark.asyncio
async def test_rename_retires_the_old_name_alias_on_the_broker(stack) -> None:
    client, listener = stack["client"], stack["listener"]
    room_id = await _make_room(client, name="Old Name")
    stack["bridge"].request_sync()
    old = f"{PREFIX}/room/old_name/temp_offset/state"
    await listener.wait_topic(old)

    await client.put(f"/api/rooms/{room_id}", json={"name": "New Name"})
    stack["bridge"].request_sync()

    await listener.wait(lambda: listener.latest.get(old) == "", "the old alias to be blanked")
    await listener.wait_topic(f"{PREFIX}/room/new_name/temp_offset/state")
    # The id tree is canonical and untouched by a rename.
    assert listener.latest[f"{PREFIX}/room/{room_id}/temp_offset/state"] != ""


@pytest.mark.asyncio
async def test_celsius_payload_converts_exactly_once_over_the_wire(stack) -> None:
    """The #231 guard, end to end on a real broker: MQTT carries the display
    value and only the REST write boundary converts."""
    client, listener = stack["client"], stack["listener"]
    room_id = await _make_room(client)
    conn = client.app["scheduler"]._db_conn
    result_topic = f"{PREFIX}/room/{room_id}/system_wide_temp/set/result"

    client.app["scheduler"]._active_unit = "C"
    await _db.set_system_setting(conn, "temperature_unit", "C")
    try:
        before = listener.count(result_topic)
        await stack["publish"](f"{PREFIX}/room/{room_id}/system_wide_temp/set", "16")
        result = await listener.wait_new(result_topic, before)
        assert json.loads(result) == {"ok": True}
    finally:
        client.app["scheduler"]._active_unit = "F"
        await _db.set_system_setting(conn, "temperature_unit", "F")

    room = await _db.get_room(conn, room_id)
    assert room is not None
    assert room.system_wide_temp == 60.8  # not 141.44 (double-converted)


@pytest.mark.asyncio
async def test_a_new_schedule_is_discovered_and_toggleable(stack) -> None:
    client, listener = stack["client"], stack["listener"]
    room_id = await _make_room(client)
    resp = await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={
            "days_of_week": [0, 1, 2, 3, 4, 5, 6],
            "start_time": "08:00",
            "end_time": "17:00",
            "target_temp": 70,
            "name": "Night setback",
        },
    )
    schedule_id = (await resp.json())["id"]
    stack["bridge"].request_sync()

    await listener.wait(
        lambda: any(t.startswith("homeassistant/") and schedule_id in t for t in listener.latest),
        "discovery for the new schedule",
    )

    await stack["publish"](f"{PREFIX}/room/{room_id}/schedule/{schedule_id}/set", "OFF")
    result = await listener.wait_topic(f"{PREFIX}/room/{room_id}/schedule/{schedule_id}/set/result")
    assert json.loads(result) == {"ok": True}

    schedules = await (await client.get(f"/api/rooms/{room_id}/schedules")).json()
    assert schedules[0]["enabled"] is False
