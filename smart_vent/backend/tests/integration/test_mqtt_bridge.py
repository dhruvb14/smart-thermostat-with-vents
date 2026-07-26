"""End-to-end MQTT bridge tests (Issue #519).

These drive the bridge against the **real** aiohttp app: the injected dispatcher
is the integration ``TestClient``, so a command genuinely travels
MQTT topic → parse → REST route → validation → SQLite, and the retained state
that comes back is rendered from what the API actually returns. Only the broker
is faked — which is the point of keeping ``aiomqtt`` behind a transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest

from backend.mqtt.bridge import MqttBridge
from backend.mqtt.config import MqttConfig

PREFIX = "plenum"
THERMO = "climate.test_thermostat"


def _config(**overrides) -> MqttConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "host": "broker",
        "port": 1883,
        "username": None,
        "password": None,
        "prefix": PREFIX,
        "discovery": True,
        "discovery_prefix": "homeassistant",
    }
    base.update(overrides)
    return MqttConfig(**base)


def _topic_matches(pattern: str, topic: str) -> bool:
    """Minimal MQTT wildcard matching, enough for the fake broker's replay."""
    pattern_parts = pattern.split("/")
    topic_parts = topic.split("/")
    for i, segment in enumerate(pattern_parts):
        if segment == "#":
            return True
        if i >= len(topic_parts):
            return False
        if segment not in ("+", topic_parts[i]):
            return False
    return len(pattern_parts) == len(topic_parts)


class FakeTransport:
    """Records publishes and lets a test feed inbound messages.

    ``broker_retained`` seeds what "the broker" already holds from an earlier
    run: like a real broker, every matching retained message is replayed (with
    the retain flag set) when a subscription arrives.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool]] = []
        self.subscriptions: list[str] = []
        self.unsubscriptions: list[str] = []
        self.broker_retained: dict[str, str] = {}
        self._inbox: asyncio.Queue = asyncio.Queue()

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self.published.append((topic, payload, retain))

    async def subscribe(self, topic: str) -> None:
        self.subscriptions.append(topic)
        for retained_topic, payload in self.broker_retained.items():
            if _topic_matches(topic, retained_topic):
                self._inbox.put_nowait((retained_topic, payload, True))

    async def unsubscribe(self, topic: str) -> None:
        self.unsubscriptions.append(topic)

    async def messages(self):
        while True:
            yield await self._inbox.get()

    def feed(self, topic: str, payload: str, retain: bool = False) -> None:
        self._inbox.put_nowait((topic, payload, retain))

    # -- assertions helpers ------------------------------------------------

    def retained(self) -> dict[str, str]:
        """Last retained payload per topic, as a broker would hold it."""
        out: dict[str, str] = {}
        for topic, payload, retain in self.published:
            if retain:
                out[topic] = payload
        return out

    def live_retained(self) -> dict[str, str]:
        """Retained topics that still have a value (blanked ones are gone)."""
        return {t: p for t, p in self.retained().items() if p != ""}

    def results(self) -> dict[str, dict]:
        return {
            topic: json.loads(payload)
            for topic, payload, retain in self.published
            if topic.endswith("/result") and not retain
        }


def client_dispatch(client):
    """Adapt the aiohttp TestClient to the bridge's dispatch signature."""

    async def dispatch(method: str, path: str, body):
        resp = await client.request(method, path, json=body)
        try:
            payload = await resp.json()
        except Exception:
            payload = None
        return resp.status, payload

    return dispatch


@pytest.fixture
async def bridge(client):
    transport = FakeTransport()
    bridge = MqttBridge(_config(), client_dispatch(client), lambda: None)
    bridge._transport = transport
    bridge.transport = transport  # convenience handle for tests
    return bridge


async def _make_room(client, name="Office", **extra) -> str:
    resp = await client.post(
        "/api/rooms",
        json={"name": name, "thermostat_entity_id": THERMO, **extra},
    )
    assert resp.status == 201, await resp.text()
    return str((await resp.json())["id"])


async def _make_thermostat(client, entity_id=THERMO, **extra) -> None:
    resp = await client.post(
        "/api/thermostats",
        json={"thermostat_entity_id": entity_id, "total_vents_count": 4, **extra},
    )
    assert resp.status in (200, 201), await resp.text()


# ---------------------------------------------------------------------------
# Commands: MQTT → REST → DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_room_field_command_writes_through_to_the_db(client, bridge, fake_ha) -> None:
    room_id = await _make_room(client)
    await bridge.handle_message(f"{PREFIX}/room/{room_id}/temp_offset/set", "1.5")

    room = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert room["temp_offset"] == 1.5
    assert bridge.transport.results()[f"{PREFIX}/room/{room_id}/temp_offset/set/result"] == {
        "ok": True
    }


@pytest.mark.asyncio
async def test_command_addressed_by_room_name(client, bridge, fake_ha) -> None:
    """#519's dual addressing: a name is far more natural to hand-type."""
    room_id = await _make_room(client, name="Living Room")
    await bridge.handle_message(f"{PREFIX}/room/living_room/temp_offset/set", "2")

    room = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert room["temp_offset"] == 2


@pytest.mark.asyncio
async def test_result_echoes_on_the_segment_the_command_used(client, bridge, fake_ha) -> None:
    await _make_room(client, name="Living Room")
    await bridge.handle_message(f"{PREFIX}/room/living_room/temp_offset/set", "2")
    assert f"{PREFIX}/room/living_room/temp_offset/set/result" in bridge.transport.results()


@pytest.mark.asyncio
async def test_celsius_payload_is_converted_exactly_once(client, bridge, fake_ha) -> None:
    """The #231 guard on a new transport: MQTT sends the display value and the
    REST write boundary converts. 16 °C must land as 60.8 °F, not 141.44 °F."""
    room_id = await _make_room(client)
    client.app["scheduler"]._active_unit = "C"
    try:
        await bridge.handle_message(f"{PREFIX}/room/{room_id}/system_wide_temp/set", "16")
    finally:
        client.app["scheduler"]._active_unit = "F"

    conn = client.app["scheduler"]._db_conn
    from backend import db as _db

    room = await _db.get_room(conn, room_id)
    assert room is not None and room.system_wide_temp == 60.8


@pytest.mark.asyncio
async def test_celsius_delta_uses_the_delta_conversion(client, bridge, fake_ha) -> None:
    """A delta has no 32° offset: 2 °C of offset is 3.6 °F, not -27.6 °F."""
    room_id = await _make_room(client)
    client.app["scheduler"]._active_unit = "C"
    try:
        await bridge.handle_message(f"{PREFIX}/room/{room_id}/temp_offset/set", "2")
    finally:
        client.app["scheduler"]._active_unit = "F"

    from backend import db as _db

    room = await _db.get_room(client.app["scheduler"]._db_conn, room_id)
    assert room is not None and room.temp_offset == 3.6


@pytest.mark.asyncio
async def test_nullable_clear_restores_inheritance(client, bridge, fake_ha) -> None:
    room_id = await _make_room(client, deadband_override=1.5)
    await bridge.handle_message(f"{PREFIX}/room/{room_id}/deadband_override/clear", "PRESS")

    room = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert room["deadband_override"] is None


@pytest.mark.asyncio
async def test_empty_payload_on_set_also_clears(client, bridge, fake_ha) -> None:
    room_id = await _make_room(client, deadband_override=1.5)
    await bridge.handle_message(f"{PREFIX}/room/{room_id}/deadband_override/set", "")

    room = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert room["deadband_override"] is None


@pytest.mark.asyncio
async def test_hold_creates_an_override(client, bridge, fake_ha) -> None:
    room_id = await _make_room(client)
    await bridge.handle_message(f"{PREFIX}/room/{room_id}/hold/set", "72")

    status = await (
        await client.post("/api/rooms/active-status", json={"room_ids": [room_id]})
    ).json()
    assert status[room_id]["source"] == "override"
    assert status[room_id]["target_temp"] == 72


@pytest.mark.asyncio
async def test_hold_clear_removes_the_override(client, bridge, fake_ha) -> None:
    room_id = await _make_room(client)
    await bridge.handle_message(f"{PREFIX}/room/{room_id}/hold/set", "72")
    await bridge.handle_message(f"{PREFIX}/room/{room_id}/hold/clear", "PRESS")

    status = await (
        await client.post("/api/rooms/active-status", json={"room_ids": [room_id]})
    ).json()
    assert status[room_id]["source"] != "override"


@pytest.mark.asyncio
async def test_thermostat_command(client, bridge, fake_ha) -> None:
    await _make_thermostat(client)
    await bridge.handle_message(f"{PREFIX}/thermostat/climate_test_thermostat/deadband/set", "0.8")

    thermostats = await (await client.get("/api/thermostats")).json()
    assert thermostats[0]["deadband"] == 0.8


@pytest.mark.asyncio
async def test_system_toggle(client, bridge, fake_ha) -> None:
    await bridge.handle_message(f"{PREFIX}/system/enabled/set", "OFF")
    assert (await (await client.get("/api/system/status")).json())["enabled"] is False


@pytest.mark.asyncio
async def test_schedule_toggle(client, bridge, fake_ha) -> None:
    room_id = await _make_room(client)
    resp = await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={
            "days_of_week": [0, 1, 2, 3, 4, 5, 6],
            "start_time": "08:00",
            "end_time": "17:00",
            "target_temp": 70,
            "name": "Day",
        },
    )
    assert resp.status == 201, await resp.text()
    schedule_id = (await resp.json())["id"]

    await bridge.handle_message(f"{PREFIX}/room/{room_id}/schedule/{schedule_id}/set", "OFF")
    schedules = await (await client.get(f"/api/rooms/{room_id}/schedules")).json()
    assert schedules[0]["enabled"] is False


# ---------------------------------------------------------------------------
# Command failures land on the result topic, never silently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_rejection_is_reported(client, bridge, fake_ha) -> None:
    """#519's driving case: a rejected write must not silently no-op."""
    room_id = await _make_room(client)
    await bridge.handle_message(f"{PREFIX}/room/{room_id}/system_wide_temp/set", "999")

    result = bridge.transport.results()[f"{PREFIX}/room/{room_id}/system_wide_temp/set/result"]
    assert result["ok"] is False
    assert "system_wide_temp" in result["error"]


@pytest.mark.asyncio
async def test_unparseable_payload_is_reported(client, bridge, fake_ha) -> None:
    room_id = await _make_room(client)
    await bridge.handle_message(f"{PREFIX}/room/{room_id}/temp_offset/set", "warm")

    result = bridge.transport.results()[f"{PREFIX}/room/{room_id}/temp_offset/set/result"]
    assert result["ok"] is False and "number" in result["error"]


@pytest.mark.asyncio
async def test_unknown_room_is_reported(client, bridge, fake_ha) -> None:
    await bridge.handle_message(f"{PREFIX}/room/nope/temp_offset/set", "1")
    result = bridge.transport.results()[f"{PREFIX}/room/nope/temp_offset/set/result"]
    assert result["ok"] is False and "unknown room" in result["error"]


@pytest.mark.asyncio
async def test_unknown_control_is_reported(client, bridge, fake_ha) -> None:
    room_id = await _make_room(client)
    await bridge.handle_message(f"{PREFIX}/room/{room_id}/not_a_control/set", "1")
    result = bridge.transport.results()[f"{PREFIX}/room/{room_id}/not_a_control/set/result"]
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_vacation_on_without_a_return_at_is_rejected(client, bridge, fake_ha) -> None:
    await bridge.handle_message(f"{PREFIX}/system/vacation_mode/set", "ON")
    result = bridge.transport.results()[f"{PREFIX}/system/vacation_mode/set/result"]
    assert result["ok"] is False and "return_at" in result["error"]


@pytest.mark.asyncio
async def test_state_topics_are_never_treated_as_commands(client, bridge, fake_ha) -> None:
    """The subscription is a wildcard, so our own retained publishes come
    straight back. Acting on one would be an infinite loop."""
    room_id = await _make_room(client)
    await bridge.handle_message(f"{PREFIX}/room/{room_id}/temp_offset/state", "9")

    room = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert room["temp_offset"] == 0
    assert bridge.transport.published == []


@pytest.mark.asyncio
async def test_every_command_publishes_a_result(client, bridge, fake_ha) -> None:
    """Success and failure alike — an automation can always tell what happened."""
    room_id = await _make_room(client)
    await bridge.handle_message(f"{PREFIX}/room/{room_id}/temp_offset/set", "1")
    await bridge.handle_message(f"{PREFIX}/room/{room_id}/temp_offset/set", "bad")
    results = bridge.transport.results()
    assert len(bridge.transport.published) == 2
    assert set(results) == {f"{PREFIX}/room/{room_id}/temp_offset/set/result"}


@pytest.mark.asyncio
async def test_results_are_not_retained(client, bridge, fake_ha) -> None:
    """A retained result would replay stale outcomes to every new subscriber."""
    room_id = await _make_room(client)
    await bridge.handle_message(f"{PREFIX}/room/{room_id}/temp_offset/set", "1")
    assert all(retain is False for _, _, retain in bridge.transport.published)


# ---------------------------------------------------------------------------
# State publishing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_publishes_state_under_both_id_and_name(client, bridge, fake_ha) -> None:
    room_id = await _make_room(client, name="Living Room", temp_offset=1.5)
    await bridge.sync()

    retained = bridge.transport.retained()
    assert retained[f"{PREFIX}/room/{room_id}/temp_offset/state"] == "1.5"
    assert retained[f"{PREFIX}/room/living_room/temp_offset/state"] == "1.5"


@pytest.mark.asyncio
async def test_state_is_published_in_display_units(client, bridge, fake_ha) -> None:
    """Stored °F, shown °C — the mirror image of the command path."""
    room_id = await _make_room(client, system_wide_temp=68)
    client.app["scheduler"]._active_unit = "C"
    try:
        await client.post("/api/settings/theme", json={"theme": "system"})
        from backend import db as _db

        await _db.set_system_setting(client.app["scheduler"]._db_conn, "temperature_unit", "C")
        await bridge.sync()
    finally:
        client.app["scheduler"]._active_unit = "F"

    retained = bridge.transport.retained()
    assert retained[f"{PREFIX}/room/{room_id}/system_wide_temp/state"] == "20"


@pytest.mark.asyncio
async def test_nullable_state_reports_the_inherited_value(client, bridge, fake_ha) -> None:
    """#519: a state topic carries the value actually in use, so a room with no
    deadband override reports its thermostat's deadband."""
    await _make_thermostat(client, deadband=0.9)
    room_id = await _make_room(client)
    await bridge.sync()

    retained = bridge.transport.retained()
    assert retained[f"{PREFIX}/room/{room_id}/deadband_override/state"] == "0.9"


@pytest.mark.asyncio
async def test_presence_target_inherits_the_thermostat_default(client, bridge, fake_ha) -> None:
    """The other non-1:1 inheritance: a room's `system_wide_temp` falls back to
    the thermostat's `default_temp`, not to a same-named field."""
    await _make_thermostat(client, default_temp=71)
    room_id = await _make_room(client)
    await bridge.sync()

    assert bridge.transport.retained()[f"{PREFIX}/room/{room_id}/system_wide_temp/state"] == "71"


@pytest.mark.asyncio
async def test_explicit_override_wins_over_inheritance(client, bridge, fake_ha) -> None:
    await _make_thermostat(client, deadband=0.9)
    room_id = await _make_room(client, deadband_override=2)
    await bridge.sync()

    assert bridge.transport.retained()[f"{PREFIX}/room/{room_id}/deadband_override/state"] == "2"


@pytest.mark.asyncio
async def test_hold_state_is_empty_without_an_override(client, bridge, fake_ha) -> None:
    room_id = await _make_room(client)
    await bridge.sync()
    assert bridge.transport.retained()[f"{PREFIX}/room/{room_id}/hold/state"] == ""


@pytest.mark.asyncio
async def test_hold_state_appears_after_a_hold(client, bridge, fake_ha) -> None:
    room_id = await _make_room(client)
    await bridge.handle_message(f"{PREFIX}/room/{room_id}/hold/set", "72")
    await bridge.sync()
    assert bridge.transport.retained()[f"{PREFIX}/room/{room_id}/hold/state"] == "72"


@pytest.mark.asyncio
async def test_state_is_retained(client, bridge, fake_ha) -> None:
    await _make_room(client)
    await bridge.sync()
    states = [p for t, p, r in bridge.transport.published if t.endswith("/state")]
    retained_states = [p for t, p, r in bridge.transport.published if t.endswith("/state") and r]
    assert states and len(states) == len(retained_states)


@pytest.mark.asyncio
async def test_unchanged_values_are_not_republished(client, bridge, fake_ha) -> None:
    """Otherwise every refresh tick rewrites the whole tree for no reason."""
    await _make_room(client)
    await bridge.sync()
    before = len(bridge.transport.published)
    await bridge.sync()
    assert len(bridge.transport.published) == before


@pytest.mark.asyncio
async def test_a_change_is_republished(client, bridge, fake_ha) -> None:
    room_id = await _make_room(client)
    await bridge.sync()
    await client.put(f"/api/rooms/{room_id}", json={"temp_offset": 3})
    await bridge.sync()
    assert bridge.transport.retained()[f"{PREFIX}/room/{room_id}/temp_offset/state"] == "3"


# ---------------------------------------------------------------------------
# Retiring topics: rename, delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_retires_the_old_name_alias(client, bridge, fake_ha) -> None:
    """#519 calls this out explicitly: a renamed room must not leave ghost
    topics behind under its old name."""
    room_id = await _make_room(client, name="Old Name")
    await bridge.sync()
    old_topic = f"{PREFIX}/room/old_name/temp_offset/state"
    assert old_topic in bridge.transport.live_retained()

    await client.put(f"/api/rooms/{room_id}", json={"name": "New Name"})
    await bridge.sync()

    live = bridge.transport.live_retained()
    assert old_topic not in live
    assert f"{PREFIX}/room/new_name/temp_offset/state" in live
    # The id tree is untouched by a rename — that is why it is canonical.
    assert f"{PREFIX}/room/{room_id}/temp_offset/state" in live


@pytest.mark.asyncio
async def test_deleting_a_room_retires_its_whole_subtree(client, bridge, fake_ha) -> None:
    room_id = await _make_room(client)
    await bridge.sync()
    assert any(f"/room/{room_id}/" in t for t in bridge.transport.live_retained())

    await client.delete(f"/api/rooms/{room_id}")
    await bridge.sync()
    assert not any(f"/room/{room_id}/" in t for t in bridge.transport.live_retained())


@pytest.mark.asyncio
async def test_deleting_a_schedule_retires_its_entity(client, bridge, fake_ha) -> None:
    room_id = await _make_room(client)
    resp = await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={
            "days_of_week": [0],
            "start_time": "08:00",
            "end_time": "09:00",
            "target_temp": 70,
        },
    )
    schedule_id = (await resp.json())["id"]
    await bridge.sync()
    assert any(schedule_id in t for t in bridge.transport.live_retained())

    await client.delete(f"/api/rooms/{room_id}/schedules/{schedule_id}")
    await bridge.sync()
    assert not any(schedule_id in t for t in bridge.transport.live_retained())


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_configs_are_published_for_a_room(client, bridge, fake_ha) -> None:
    room_id = await _make_room(client)
    await bridge.sync()

    configs = {
        t: json.loads(p)
        for t, p in bridge.transport.live_retained().items()
        if t.startswith("homeassistant/")
    }
    assert configs
    payloads = list(configs.values())
    assert any(p.get("command_topic", "").endswith(f"/room/{room_id}/hold/set") for p in payloads)
    # Never the name alias — HA's registry must be rename-proof.
    assert all("/room/office/" not in p.get("command_topic", "") for p in payloads)


@pytest.mark.asyncio
async def test_discovery_can_be_turned_off(client, bridge, fake_ha) -> None:
    bridge._config = _config(discovery=False)
    await _make_room(client)
    await bridge.sync()
    assert not any(t.startswith("homeassistant/") for t in bridge.transport.retained())


@pytest.mark.asyncio
async def test_a_new_schedule_is_discovered_dynamically(client, bridge, fake_ha) -> None:
    room_id = await _make_room(client)
    await bridge.sync()
    before = set(bridge.transport.live_retained())

    resp = await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={
            "days_of_week": [0],
            "start_time": "08:00",
            "end_time": "09:00",
            "target_temp": 70,
            "name": "Night setback",
        },
    )
    schedule_id = (await resp.json())["id"]
    await bridge.sync()
    added = set(bridge.transport.live_retained()) - before

    assert any(t.startswith("homeassistant/switch/") and schedule_id in t for t in added)
    configs = [
        json.loads(bridge.transport.live_retained()[t]) for t in added if t.startswith("home")
    ]
    assert any(c["name"] == "Schedule: Night setback" for c in configs)


@pytest.mark.asyncio
async def test_unnamed_schedule_falls_back_to_its_id(client, bridge, fake_ha) -> None:
    """#520 made the name optional; the GUID is the documented fallback."""
    room_id = await _make_room(client)
    resp = await client.post(
        f"/api/rooms/{room_id}/schedules",
        json={
            "days_of_week": [0],
            "start_time": "08:00",
            "end_time": "09:00",
            "target_temp": 70,
        },
    )
    schedule_id = (await resp.json())["id"]
    await bridge.sync()

    configs = [
        json.loads(p)
        for t, p in bridge.transport.live_retained().items()
        if t.startswith("homeassistant/") and schedule_id in t
    ]
    assert configs and configs[0]["name"] == f"Schedule: {schedule_id}"


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_announces_availability_and_subscribes(client, fake_ha) -> None:
    transport = FakeTransport()

    class _Conn:
        async def __aenter__(self):
            return transport

        async def __aexit__(self, *a):
            return False

    bridge = MqttBridge(_config(), client_dispatch(client), lambda: _Conn(), refresh_seconds=0.05)
    task = asyncio.create_task(bridge.run())
    await asyncio.sleep(0.15)
    bridge.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert (f"{PREFIX}/status", "online", True) in transport.published
    assert set(transport.subscriptions) == {
        f"{PREFIX}/room/#",
        f"{PREFIX}/thermostat/#",
        f"{PREFIX}/system/#",
        # Watched only until the reconcile sweep runs (see the reconcile tests).
        "homeassistant/+/+/config",
    }


@pytest.mark.asyncio
async def test_run_does_not_connect_while_disabled(client, fake_ha) -> None:
    """The Settings toggle gates the connection, exactly like the MCP one."""
    connections = []

    class _Conn:
        async def __aenter__(self):
            connections.append(1)
            return FakeTransport()

        async def __aexit__(self, *a):
            return False

    bridge = MqttBridge(
        _config(),
        client_dispatch(client),
        lambda: _Conn(),
        is_enabled=lambda: False,
        refresh_seconds=0.05,
    )
    task = asyncio.create_task(bridge.run())
    await asyncio.sleep(0.1)
    bridge.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert connections == []
    assert bridge.connected is False


@pytest.mark.asyncio
async def test_a_broker_failure_never_escapes_the_bridge(client, fake_ha) -> None:
    """HVAC control does not depend on MQTT; a dead broker must not propagate."""
    attempts = []

    class _Conn:
        async def __aenter__(self):
            attempts.append(1)
            raise OSError("broker unreachable")

        async def __aexit__(self, *a):
            return False

    bridge = MqttBridge(_config(), client_dispatch(client), lambda: _Conn())
    task = asyncio.create_task(bridge.run())
    await asyncio.sleep(0.1)
    bridge.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert attempts, "the bridge should have tried to connect"
    assert bridge.last_error is not None and "broker unreachable" in bridge.last_error


@pytest.mark.asyncio
async def test_a_bad_message_does_not_kill_the_read_loop(client, bridge, fake_ha) -> None:
    """One malformed command must not take the whole connection down."""
    room_id = await _make_room(client)
    await bridge.handle_message(f"{PREFIX}/room/{room_id}/temp_offset/set", "nonsense")
    await bridge.handle_message(f"{PREFIX}/room/{room_id}/temp_offset/set", "2")

    room = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert room["temp_offset"] == 2


@pytest.mark.asyncio
async def test_a_malformed_message_does_not_end_the_session(client, fake_ha) -> None:
    """The read loop swallows per-message failures; one bad publisher must not
    disconnect the bridge and stall every other automation."""
    transport = FakeTransport()

    class _Conn:
        async def __aenter__(self):
            return transport

        async def __aexit__(self, *a):
            return False

    bridge = MqttBridge(_config(), client_dispatch(client), lambda: _Conn(), refresh_seconds=5)
    room_id = await _make_room(client)
    task = asyncio.create_task(bridge.run())
    await asyncio.sleep(0.1)

    transport.feed(f"{PREFIX}/room/{room_id}/temp_offset/set", "not-a-number")
    transport.feed(f"{PREFIX}/room/{room_id}/temp_offset/set", "4")
    await asyncio.sleep(0.2)

    bridge.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    room = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert room["temp_offset"] == 4


@pytest.mark.asyncio
async def test_turning_the_toggle_off_disconnects(client, fake_ha) -> None:
    """Flipping the Settings switch must actually drop the connection, not just
    stop new work — otherwise "off" would still hold a broker session."""
    enabled = {"value": True}
    sessions = []

    class _Conn:
        async def __aenter__(self):
            sessions.append(1)
            return FakeTransport()

        async def __aexit__(self, *a):
            return False

    bridge = MqttBridge(
        _config(),
        client_dispatch(client),
        lambda: _Conn(),
        is_enabled=lambda: enabled["value"],
        refresh_seconds=0.05,
    )
    task = asyncio.create_task(bridge.run())
    await asyncio.sleep(0.15)
    assert bridge.connected is True

    enabled["value"] = False
    bridge.request_sync()
    await asyncio.sleep(0.2)
    assert bridge.connected is False

    bridge.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_reconnect_republishes_everything(client, fake_ha) -> None:
    """A new broker session knows nothing of what the old one retained, so the
    change-suppression cache must be dropped on connect."""
    transports = []

    class _Conn:
        def __init__(self) -> None:
            self.transport = FakeTransport()
            transports.append(self.transport)

        async def __aenter__(self):
            return self.transport

        async def __aexit__(self, *a):
            return False

    await _make_room(client)
    bridge = MqttBridge(_config(), client_dispatch(client), lambda: _Conn(), refresh_seconds=5)

    # Two sessions in a row: both must publish a full tree.
    for _ in range(2):
        conn = _Conn()
        transport = conn.transport
        bridge._transport = transport
        bridge._retained.clear()
        await bridge.sync()
        assert transport.live_retained(), "a fresh session must republish state"


@pytest.mark.asyncio
async def test_sync_without_a_connection_is_a_no_op(client, fake_ha) -> None:
    """A refresh can fire while the broker is down; it must not explode."""
    bridge = MqttBridge(_config(), client_dispatch(client), lambda: None)
    await bridge.sync()  # no transport attached


@pytest.mark.asyncio
async def test_a_failing_api_read_does_not_break_the_sync(client, bridge, fake_ha) -> None:
    """If the REST API answers an error mid-sync, publish what we can rather
    than aborting and leaving the tree half-written."""

    async def _failing(method, path, body):
        if path == "/api/rooms":
            return 500, {"error": "boom"}
        return await client_dispatch(client)(method, path, body)

    bridge._dispatch = _failing
    await bridge.sync()
    # The system device still got published even though rooms failed.
    assert f"{PREFIX}/system/enabled/state" in bridge.transport.retained()


# ---------------------------------------------------------------------------
# Partial snapshots: a failed read must never masquerade as a deletion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_rooms_read_does_not_retire_the_tree(client, bridge, fake_ha) -> None:
    """A transient 500 on GET /api/rooms once made the snapshot claim "zero
    rooms", and the retire sweep then blanked every room state topic AND every
    room discovery config — which deletes the entities from Home Assistant.
    A partial snapshot must skip the sweep entirely."""
    room_id = await _make_room(client)
    await bridge.sync()
    state_topic = f"{PREFIX}/room/{room_id}/temp_offset/state"
    config_topics = [
        t
        for t in bridge.transport.live_retained()
        if t.startswith("homeassistant/") and room_id in t
    ]
    assert state_topic in bridge.transport.live_retained()
    assert config_topics, "the room should have discovery configs to protect"

    real = client_dispatch(client)

    async def failing(method, path, body):
        if method == "GET" and path == "/api/rooms":
            return 500, {"error": "boom"}
        return await real(method, path, body)

    bridge._dispatch = failing
    await bridge.sync()

    retained = bridge.transport.live_retained()
    assert state_topic in retained, "room state must survive a failed read"
    for topic in config_topics:
        assert topic in retained, "discovery configs must survive a failed read"


@pytest.mark.asyncio
async def test_a_real_deletion_is_still_retired_after_recovery(client, bridge, fake_ha) -> None:
    """The partial-snapshot gate must not wedge: once reads succeed again, a
    genuine deletion retires exactly as before."""
    room_id = await _make_room(client)
    await bridge.sync()

    real = client_dispatch(client)

    async def failing(method, path, body):
        if method == "GET" and path == "/api/rooms":
            return 500, {"error": "boom"}
        return await real(method, path, body)

    bridge._dispatch = failing
    await bridge.sync()  # partial — nothing retired

    bridge._dispatch = real
    resp = await client.delete(f"/api/rooms/{room_id}")
    assert resp.status == 200
    await bridge.sync()  # full again — the deletion goes through

    assert not any(room_id in t for t in bridge.transport.live_retained())


@pytest.mark.asyncio
async def test_a_failed_active_status_read_keeps_the_hold_state(client, bridge, fake_ha) -> None:
    """Hold state comes from a separate POST; if that one read fails the hold
    topic must keep its retained value, not blank out to "no hold"."""
    room_id = await _make_room(client)
    resp = await client.post(f"/api/rooms/{room_id}/override", json={"target_temp": 72})
    assert resp.status == 200, await resp.text()
    await bridge.sync()
    hold_topic = f"{PREFIX}/room/{room_id}/hold/state"
    assert bridge.transport.live_retained()[hold_topic] == "72"

    real = client_dispatch(client)

    async def failing(method, path, body):
        if path == "/api/rooms/active-status":
            return 500, {"error": "boom"}
        return await real(method, path, body)

    bridge._dispatch = failing
    await bridge.sync()

    assert bridge.transport.live_retained()[hold_topic] == "72"


@pytest.mark.asyncio
async def test_a_failed_settings_read_keeps_the_last_known_unit(client, bridge, fake_ha) -> None:
    """The settings read carries the display unit. If it fails, the last-known
    unit must be reused — defaulting to °F would republish a °C install's
    whole tree with °F numbers for one cycle."""
    room_id = await _make_room(client)
    resp = await client.put(f"/api/rooms/{room_id}", json={"temp_offset": 3.6})  # 3.6 °F stored
    assert resp.status == 200
    from backend import db as _db

    conn = client.app["scheduler"]._db_conn
    client.app["scheduler"]._active_unit = "C"
    try:
        await _db.set_system_setting(conn, "temperature_unit", "C")
        await bridge.sync()
        offset_topic = f"{PREFIX}/room/{room_id}/temp_offset/state"
        assert bridge.transport.live_retained()[offset_topic] == "2"  # 3.6 °F Δ = 2 °C Δ

        real = client_dispatch(client)

        async def failing(method, path, body):
            if method == "GET" and path == "/api/settings":
                return 500, {"error": "boom"}
            return await real(method, path, body)

        bridge._dispatch = failing
        await bridge.sync()
        assert bridge.transport.live_retained()[offset_topic] == "2", (
            "a failed settings read must not flip the tree back to °F"
        )
    finally:
        client.app["scheduler"]._active_unit = "F"
        await _db.set_system_setting(conn, "temperature_unit", "F")


# ---------------------------------------------------------------------------
# Retained inbound messages: broker replays are never commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_retained_command_is_not_executed(client, fake_ha) -> None:
    """A message published with retain=true is replayed by the broker on every
    subsequent connect. Executing the replay would re-apply a stale command
    after every restart, forever — so retained messages never dispatch."""
    transport = FakeTransport()

    class _Conn:
        async def __aenter__(self):
            return transport

        async def __aexit__(self, *a):
            return False

    room_id = await _make_room(client)
    bridge = MqttBridge(_config(), client_dispatch(client), lambda: _Conn(), refresh_seconds=5)
    task = asyncio.create_task(bridge.run())
    await asyncio.sleep(0.1)

    transport.feed(f"{PREFIX}/room/{room_id}/temp_offset/set", "5", retain=True)
    transport.feed(f"{PREFIX}/room/{room_id}/presence_holdover_hours/set", "3")  # live control
    await asyncio.sleep(0.2)

    bridge.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    room = await (await client.get(f"/api/rooms/{room_id}")).json()
    assert room["temp_offset"] == 0, "the retained replay must not have executed"
    assert room["presence_holdover_hours"] == 3, "the live command right after it must have"
    assert not any(
        topic.endswith("temp_offset/set/result") for topic, _, _ in transport.published
    ), "a dropped replay gets no result — it is not an attempt"


# ---------------------------------------------------------------------------
# Reconcile at connect: stale retained topics from an earlier run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_blanks_stale_topics_from_an_earlier_run(client, fake_ha) -> None:
    """`_retained` is this process's memory; a room deleted while the bridge
    was disconnected (or a whole earlier install) leaves retained state and
    discovery configs on the broker that no session ever knew it published.
    The first refresh after connect sweeps them — and ONLY ours."""
    transport = FakeTransport()
    ghost_state = f"{PREFIX}/room/ghost/temp_offset/state"
    ghost_command = f"{PREFIX}/room/ghost/hold/set"
    ghost_config = f"homeassistant/number/{PREFIX}_room_ghost_temp_offset/config"
    beta_config = f"homeassistant/number/{PREFIX}_beta_room_x_temp_offset/config"
    foreign_config = "homeassistant/light/zigbee_lamp/config"
    transport.broker_retained = {
        ghost_state: "2",
        ghost_command: "72",
        ghost_config: "{}",
        beta_config: "{}",
        foreign_config: "{}",
    }

    class _Conn:
        async def __aenter__(self):
            return transport

        async def __aexit__(self, *a):
            return False

    room_id = await _make_room(client)
    bridge = MqttBridge(_config(), client_dispatch(client), lambda: _Conn(), refresh_seconds=0.05)
    task = asyncio.create_task(bridge.run())
    await asyncio.sleep(0.5)
    bridge.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    blanked = {t for t, p, retain in transport.published if retain and p == ""}
    assert ghost_state in blanked, "stale state from an earlier run must be retired"
    assert ghost_command in blanked, "a stale retained command must be cleared, not executed"
    assert ghost_config in blanked, "a stale discovery config must be retired"
    assert beta_config not in blanked, "another instance's configs are not ours to touch"
    assert foreign_config not in blanked, "other integrations' configs are never touched"
    assert f"{PREFIX}/room/{room_id}/temp_offset/state" in transport.live_retained(), (
        "live topics must survive the sweep"
    )
    assert "homeassistant/+/+/config" in transport.unsubscriptions, (
        "the discovery watch ends once the sweep has run"
    )
    assert not any(
        topic.endswith("/result") and "/ghost/" in topic for topic, _, _ in transport.published
    ), "the stale retained command must have been cleared, never dispatched"
