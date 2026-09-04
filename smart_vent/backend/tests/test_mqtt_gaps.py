"""Edge paths of the MQTT runtime that the happy-path suites never reach (#519).

The bridge's job is to be unkillable: a dead broker, a crashing handler, a
half-failed REST read, a stale retained topic from a previous install — none of
them may take the add-on down or corrupt what Home Assistant sees. Those are
exactly the branches an end-to-end test flies past, so they are driven directly
here against a fake transport and a fake REST world.

Timing is faked, never slept: :class:`_FakeAsyncio` swaps the bridge module's
``asyncio.sleep`` for a recorder, so the reconnect backoff is asserted as an
exact sequence of delays instead of measured off the wall clock.

Contract reminder (CLAUDE.md / #519): MQTT never converts temperatures on the
command path, and state-side °F → display conversion happens in exactly one
place (``MqttBridge._display``). Nothing here asserts a conversion anywhere else.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from backend.mqtt import bridge as bridge_mod
from backend.mqtt import commands, topics
from backend.mqtt.bridge import MqttBridge, Snapshot
from backend.mqtt.commands import CommandError, build_request, decode_value
from backend.mqtt.config import MqttConfig, load_config
from backend.mqtt.registry import (
    DEVICE_ROOM,
    DEVICE_SYSTEM,
    DEVICE_THERMOSTAT,
    KIND_ACTION,
    KIND_BOOL,
    Control,
    control_for,
)

PREFIX = "plenum"
THERMO = "climate.upstairs"
THERMO_IDENT = "climate_upstairs"


def _config(**overrides) -> MqttConfig:
    base: dict[str, Any] = {
        "host": "broker.local",
        "port": 1883,
        "username": None,
        "password": None,
        "prefix": PREFIX,
        "discovery": True,
        "discovery_prefix": "homeassistant",
    }
    base.update(overrides)
    return MqttConfig(**base)


class FakeApi:
    """A stand-in for the loopback REST dispatcher.

    Reads answer from in-memory tables; anything else counts as a write and
    returns ``write_result`` (or raises ``write_raises``, to simulate the
    loopback itself blowing up rather than answering an error status).
    """

    def __init__(self, rooms=None, thermostats=None) -> None:
        self.rooms: list[dict] = list(rooms or [])
        self.thermostats: list[dict] = list(thermostats or [])
        self.settings: dict = {"temperature_unit": "F"}
        self.system: dict = {"enabled": True}
        self.writes: list[tuple[str, str, Any]] = []
        self.write_result: tuple[int, Any] = (200, {})
        self.write_raises: BaseException | None = None

    async def __call__(self, method: str, path: str, body):
        if method == "GET":
            if path == "/api/settings":
                return 200, self.settings
            if path == "/api/rooms":
                return 200, self.rooms
            if path == "/api/thermostats":
                return 200, self.thermostats
            if path == "/api/system/status":
                return 200, self.system
            if path.startswith("/api/rooms/") and path.endswith("/schedules"):
                return 200, []
        if method == "POST" and path == "/api/rooms/active-status":
            return 200, {}
        self.writes.append((method, path, body))
        if self.write_raises is not None:
            raise self.write_raises
        return self.write_result


class FakeTransport:
    """Records publishes and replays a fixed script of inbound messages."""

    def __init__(self, inbound: list[tuple[str, str, bool]] | None = None) -> None:
        self.published: list[tuple[str, str, bool]] = []
        self.subscriptions: list[str] = []
        self.unsubscriptions: list[str] = []
        self.inbound = list(inbound or [])

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self.published.append((topic, payload, retain))

    async def subscribe(self, topic: str) -> None:
        self.subscriptions.append(topic)

    async def unsubscribe(self, topic: str) -> None:
        self.unsubscriptions.append(topic)

    async def messages(self):
        for message in self.inbound:
            yield message

    def results(self) -> dict[str, dict]:
        return {
            topic: json.loads(payload)
            for topic, payload, retain in self.published
            if topic.endswith("/result") and not retain
        }


class _FakeAsyncio:
    """``asyncio`` with a recording, instantaneous ``sleep``.

    Substituted for the bridge module's ``asyncio`` so the reconnect backoff can
    be asserted exactly. Everything else (``CancelledError``, ``create_task``,
    ``wait``, ``wait_for``, ``Event``) falls through to the real module.
    """

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __getattr__(self, name: str):
        return getattr(asyncio, name)

    async def sleep(self, delay: float, *args, **kwargs):
        self.sleeps.append(delay)
        return await asyncio.sleep(0)


def _make_bridge(api: FakeApi | None = None, **kwargs) -> tuple[MqttBridge, FakeApi]:
    api = api or FakeApi()
    bridge = MqttBridge(_config(), api, lambda: None, **kwargs)
    return bridge, api


def _connected(api: FakeApi | None = None) -> tuple[MqttBridge, FakeApi, FakeTransport]:
    bridge, api = _make_bridge(api)
    transport = FakeTransport()
    bridge._transport = transport
    return bridge, api, transport


# ---------------------------------------------------------------------------
# Snapshot addressing
# ---------------------------------------------------------------------------


class TestSnapshotLookup:
    def test_thermostat_lookup_answers_none_when_nothing_matches(self) -> None:
        """A command for a thermostat that is not configured must resolve to
        "unknown", not to whichever thermostat happens to be first."""
        snapshot = Snapshot(thermostats=[{"thermostat_entity_id": THERMO}])
        assert snapshot.thermostat_by_ident(THERMO) is not None
        assert snapshot.thermostat_by_ident(THERMO_IDENT) is not None
        assert snapshot.thermostat_by_ident("climate.somewhere_else") is None

    def test_a_thermostat_row_without_an_entity_id_never_matches(self) -> None:
        snapshot = Snapshot(thermostats=[{"name": "orphan"}])
        assert snapshot.thermostat_by_ident("orphan") is None

    def test_a_room_name_is_matched_past_earlier_non_matches(self) -> None:
        """Ids win outright, and the name pass has to scan the whole list —
        stopping at the first room would make the alias tree order-dependent."""
        snapshot = Snapshot(
            rooms=[
                {"id": "guid-a", "name": "Office"},
                {"id": "guid-b", "name": "Living Room"},
            ]
        )
        assert snapshot.room_by_ident("guid-b") == {"id": "guid-b", "name": "Living Room"}
        assert snapshot.room_by_ident("living_room") == {"id": "guid-b", "name": "Living Room"}
        assert snapshot.room_by_ident("basement") is None


# ---------------------------------------------------------------------------
# The reconnect loop
# ---------------------------------------------------------------------------


class TestRunLoop:
    async def test_the_loop_keeps_polling_while_the_toggle_is_off(self) -> None:
        """ "Off" is a poll, not an exit: flipping the Settings switch back on
        has to reconnect without restarting the add-on."""
        checks: list[int] = []
        connections: list[int] = []

        def _enabled() -> bool:
            checks.append(1)
            # Wake the idle immediately so the poll loop turns without sleeping;
            # after a few turns, end the run.
            (bridge.stop if len(checks) >= 4 else bridge.request_sync)()
            return False

        class _Conn:
            async def __aenter__(self):
                connections.append(1)
                return FakeTransport()

            async def __aexit__(self, *exc):
                return False

        api = FakeApi()
        bridge = MqttBridge(_config(), api, lambda: _Conn(), is_enabled=_enabled)
        await asyncio.wait_for(bridge.run(), timeout=5)

        assert len(checks) == 4, "the disabled branch must loop, not fall through or exit"
        assert connections == [], "a disabled bridge must never open a broker session"
        assert bridge.connected is False

    async def test_the_backoff_doubles_and_is_capped(self, monkeypatch) -> None:
        """A broker that is down for hours must not be retried in a hot loop,
        and must not back off past a minute either — HVAC keeps running, but the
        bridge has to come back promptly once the broker does."""
        fake = _FakeAsyncio()
        monkeypatch.setattr(bridge_mod, "asyncio", fake)
        attempts: list[int] = []

        class _Conn:
            async def __aenter__(self):
                attempts.append(1)
                if len(attempts) >= 8:
                    bridge.stop()
                raise OSError("broker unreachable")

            async def __aexit__(self, *exc):
                return False

        bridge = MqttBridge(_config(), FakeApi(), lambda: _Conn())
        await asyncio.wait_for(bridge.run(), timeout=5)

        assert fake.sleeps == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
        assert len(attempts) == 8
        # The 8th attempt failed *after* stop(): it breaks out instead of
        # sleeping again, so the last error is still the 7th attempt's.
        assert bridge.last_error is not None
        assert "broker unreachable" in bridge.last_error

    async def test_a_clean_session_resets_the_backoff(self, monkeypatch) -> None:
        """After a session that actually connected, the next failure must wait
        the minimum again — otherwise a flapping broker would inherit the old
        backoff and take a minute to come back every time."""
        fake = _FakeAsyncio()
        monkeypatch.setattr(bridge_mod, "asyncio", fake)
        transport = FakeTransport()
        attempts: list[int] = []

        class _Conn:
            async def __aenter__(self):
                attempts.append(1)
                if len(attempts) >= 5:
                    bridge.stop()
                    raise OSError("stopping")
                if len(attempts) == 3:
                    return transport  # one good session
                raise OSError("broker unreachable")

            async def __aexit__(self, *exc):
                return False

        bridge = MqttBridge(_config(), FakeApi(), lambda: _Conn())
        await asyncio.wait_for(bridge.run(), timeout=5)

        assert fake.sleeps == [2.0, 4.0, 2.0], (
            "the delay after the good session must be the minimum again, not 8.0"
        )
        assert (f"{PREFIX}/status", "online", True) in transport.published
        assert bridge.connected is False, "the transport is dropped when the session ends"


# ---------------------------------------------------------------------------
# The read loop
# ---------------------------------------------------------------------------


class TestReadLoop:
    async def test_messages_arriving_after_stop_are_not_executed(self) -> None:
        """Shutdown must not apply one last command out of the socket buffer."""
        api = FakeApi(rooms=[{"id": "r1", "name": "Office"}])
        bridge, api, transport = _connected(api)
        inbound = FakeTransport([(f"{PREFIX}/room/r1/temp_offset/set", "3", False)])

        bridge.stop()
        await bridge._read_loop(inbound)

        assert api.writes == []
        assert transport.published == [], "a dropped message gets no result either"

    async def test_a_crashing_handler_is_logged_and_the_next_message_still_runs(
        self, caplog
    ) -> None:
        """One malformed command (or one loopback hiccup) must never drop the
        connection and stall every other automation."""
        api = FakeApi(rooms=[{"id": "r1", "name": "Office"}])
        bridge, api, transport = _connected(api)
        api.write_raises = RuntimeError("loopback exploded")
        first = f"{PREFIX}/room/r1/temp_offset/set"
        second = f"{PREFIX}/room/r1/presence_holdover_hours/set"

        async def _messages():
            yield (first, "3", False)
            api.write_raises = None  # the loopback recovers
            yield (second, "4", False)

        inbound = FakeTransport()
        inbound.messages = _messages  # type: ignore[method-assign]

        with caplog.at_level(logging.ERROR, logger="backend.mqtt.bridge"):
            await bridge._read_loop(inbound)

        assert "Failed to handle MQTT message" in caplog.text
        assert api.writes == [
            ("PUT", "/api/rooms/r1", {"temp_offset": 3.0}),
            ("PUT", "/api/rooms/r1", {"presence_holdover_hours": 4.0}),
        ], "the second command must still have reached REST"
        assert transport.results() == {f"{second}/result": {"ok": True}}, (
            "the crashed command gets no verdict; the recovered one does"
        )

    async def test_a_cancellation_inside_a_handler_is_not_swallowed(self) -> None:
        """The broad `except` must not turn shutdown into a logged warning and
        leave the read loop spinning."""
        api = FakeApi(rooms=[{"id": "r1", "name": "Office"}])
        bridge, api, _ = _connected(api)
        api.write_raises = asyncio.CancelledError()
        inbound = FakeTransport([(f"{PREFIX}/room/r1/temp_offset/set", "3", False)])

        with pytest.raises(asyncio.CancelledError):
            await bridge._read_loop(inbound)


# ---------------------------------------------------------------------------
# What counts as a broker replay worth reconciling
# ---------------------------------------------------------------------------


class TestBrokerRetainedCollection:
    async def test_replays_are_ignored_once_the_sweep_has_run(self) -> None:
        """`_broker_retained` exists only to feed the one-shot reconcile. Left
        collecting, it would grow for the life of the process."""
        bridge, _, _ = _connected()
        assert bridge._reconciled is True  # nothing to reconcile before a connect
        inbound = FakeTransport([(f"{PREFIX}/room/r1/temp_offset/state", "3", True)])

        await bridge._read_loop(inbound)

        assert bridge._broker_retained == {}

    async def test_an_already_blank_replay_is_not_collected(self) -> None:
        """A blanked topic is already retired; collecting it would have the
        sweep publish another blank over a topic nobody holds."""
        bridge, _, _ = _connected()
        bridge._reconciled = False
        inbound = FakeTransport([(f"{PREFIX}/room/r1/temp_offset/state", "", True)])

        await bridge._read_loop(inbound)

        assert bridge._broker_retained == {}

    async def test_only_our_own_discovery_configs_are_collected(self) -> None:
        """The config wildcard sees every integration on the broker. Collecting
        someone else's config would have the sweep blank it — deleting another
        integration's HA entity."""
        bridge, _, _ = _connected()
        bridge._reconciled = False
        ours = f"homeassistant/number/{PREFIX}_room_r1_temp_offset/config"
        inbound = FakeTransport(
            [
                # Not four segments — not a config topic at all.
                (f"homeassistant/number/{PREFIX}_room_r1_temp_offset", "{}", True),
                (f"homeassistant/number/{PREFIX}_room_r1_temp_offset/config/extra", "{}", True),
                # Right shape, wrong last segment.
                (f"homeassistant/number/{PREFIX}_room_r1_temp_offset/state", "{}", True),
                # Right shape, a different discovery prefix — another HA install.
                (f"other_discovery/number/{PREFIX}_room_r1_temp_offset/config", "{}", True),
                # A neighbouring integration's config under our discovery prefix.
                ("homeassistant/light/zigbee_lamp/config", "{}", True),
                (ours, '{"device": {}}', True),
            ]
        )

        await bridge._read_loop(inbound)

        assert set(bridge._broker_retained) == {ours}


# ---------------------------------------------------------------------------
# The refresh loop and the reconcile sweep
# ---------------------------------------------------------------------------


class TestRefreshLoop:
    async def test_a_failing_sync_does_not_end_the_session(self, caplog) -> None:
        """A transient REST failure mid-refresh must be logged and retried on
        the next tick, not end the broker session."""
        bridge, _, _ = _connected()
        bridge._refresh_seconds = 0
        calls: list[int] = []

        async def _boom():
            calls.append(1)
            if len(calls) >= 3:
                bridge.stop()
            raise RuntimeError("sync exploded")

        bridge.sync = _boom  # type: ignore[method-assign]

        with caplog.at_level(logging.ERROR, logger="backend.mqtt.bridge"):
            await asyncio.wait_for(bridge._refresh_loop(), timeout=5)

        assert len(calls) == 3, "the loop must survive a failure and tick again"
        assert "MQTT state sync failed" in caplog.text

    async def test_a_cancellation_mid_sync_is_not_swallowed(self) -> None:
        bridge, _, _ = _connected()
        bridge._refresh_seconds = 0

        async def _cancelled():
            raise asyncio.CancelledError

        bridge.sync = _cancelled  # type: ignore[method-assign]

        with pytest.raises(asyncio.CancelledError):
            await bridge._refresh_loop()

    async def test_the_sweep_is_skipped_when_the_broker_is_gone(self) -> None:
        """The refresh that triggers the sweep can land after the connection
        dropped. Reconciling then would mark the session reconciled without
        having blanked anything, so the stale topics would survive the
        reconnect too."""
        bridge, _, _ = _connected()
        bridge._transport = None
        bridge._reconciled = False
        bridge._broker_retained = {f"{PREFIX}/room/ghost/temp_offset/state": "2"}

        await bridge._reconcile_stale()

        assert bridge._reconciled is False, "the sweep must be retried on the next refresh"
        assert bridge._broker_retained != {}


class TestDeviceMoveMigration:
    """Re-registering an entity is destructive (HA drops it, then re-adds it),
    so it may only fire when BOTH configs were understood and their device
    identifiers genuinely differ."""

    async def test_an_unreadable_retained_config_never_triggers_a_re_registration(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(bridge_mod, "_MIGRATE_REPUBLISH_DELAY_S", 0)
        bridge, _, transport = _connected()

        def _topic(name: str) -> str:
            return f"homeassistant/number/{PREFIX}_room_{name}_temp_offset/config"

        current = json.dumps({"device": {"identifiers": [f"{PREFIX}_room_new"]}})
        bad = {
            _topic("not_json"): "}{ not json",
            _topic("not_an_object"): "[1, 2]",
            _topic("identifiers_not_a_list"): json.dumps({"device": {"identifiers": "one"}}),
            _topic("no_device_block"): json.dumps({"name": "x"}),
        }
        moved_topic = _topic("moved")
        bridge._broker_retained = {
            **bad,
            moved_topic: json.dumps({"device": {"identifiers": [f"{PREFIX}_room_old"]}}),
        }
        bridge._retained = dict.fromkeys([*bad, moved_topic], current)

        await bridge._migrate_moved_devices(transport)

        touched = {topic for topic, _, _ in transport.published}
        assert touched == {moved_topic}, (
            "only the config whose device identifiers were BOTH readable and different "
            "may be re-registered"
        )
        assert [p for t, p, _ in transport.published if t == moved_topic] == ["", current]

    async def test_a_current_config_that_cannot_be_parsed_is_left_alone(self, monkeypatch) -> None:
        monkeypatch.setattr(bridge_mod, "_MIGRATE_REPUBLISH_DELAY_S", 0)
        bridge, _, transport = _connected()
        topic = f"homeassistant/number/{PREFIX}_room_r1_temp_offset/config"
        bridge._broker_retained = {
            topic: json.dumps({"device": {"identifiers": [f"{PREFIX}_room_old"]}})
        }
        bridge._retained = {topic: "not json either"}

        await bridge._migrate_moved_devices(transport)

        assert transport.published == []


# ---------------------------------------------------------------------------
# Rendering state
# ---------------------------------------------------------------------------


class TestDesiredState:
    def test_a_missing_temperature_has_no_display_value(self) -> None:
        """Nothing set and nothing inherited is not 32 °C — it stays unknown."""
        bridge, _ = _make_bridge()
        bridge._config_unit = "C"
        control = control_for(DEVICE_ROOM, "system_wide_temp")
        assert control is not None
        assert bridge._display(control, None) is None
        assert bridge._display(control, 68.0) == 20.0

    def test_a_thermostat_action_control_publishes_no_state_topic(self, monkeypatch) -> None:
        """`has_state` is what keeps a button off the state tree — a retained
        state topic for a press would show up in HA as a stuck value."""
        bridge, _ = _make_bridge()
        button = Control(
            key="reset_something",
            entity="button",
            name="Reset Something",
            kind=KIND_ACTION,
            special="presence_clear",
        )
        real = control_for(DEVICE_THERMOSTAT, "deadband")
        assert real is not None
        monkeypatch.setattr(bridge_mod, "THERMOSTAT_CONTROLS", (button, real))

        out = bridge.desired_state(
            Snapshot(thermostats=[{"thermostat_entity_id": THERMO, "deadband": 1.0}])
        )

        assert f"{PREFIX}/thermostat/{THERMO_IDENT}/deadband/state" in out
        assert f"{PREFIX}/thermostat/{THERMO_IDENT}/reset_something/state" not in out

    def test_a_room_with_no_usable_name_gets_only_the_id_tree(self) -> None:
        """The alias segment is the sanitised name. When there is nothing left
        after sanitising — or it would collide with the GUID — the room answers
        on its id alone rather than on an empty topic segment."""
        bridge, _ = _make_bridge()

        blank = bridge.desired_state(Snapshot(rooms=[{"id": "guid-a", "name": "!!!"}]))
        assert any(t.startswith(f"{PREFIX}/room/guid-a/") for t in blank)
        assert all(t.split("/")[2] == "guid-a" for t in blank if t.startswith(f"{PREFIX}/room/"))

        same = bridge.desired_state(Snapshot(rooms=[{"id": "office", "name": "Office"}]))
        assert all(t.split("/")[2] == "office" for t in same if t.startswith(f"{PREFIX}/room/"))

    def test_system_enabled_is_omitted_when_its_read_failed(self) -> None:
        """A failed `/api/system/status` must leave the retained switch alone —
        publishing the `True` default would show the system as ON while it is
        actually off."""
        snapshot = Snapshot(system_enabled=True, vacation={"enabled": False}, failed={"system"})
        bridge, _ = _make_bridge()

        out = bridge.desired_state(snapshot)

        assert f"{PREFIX}/system/enabled/state" not in out
        assert out[f"{PREFIX}/system/vacation_mode/state"] == "OFF"

    def test_a_failed_settings_read_omits_the_vacation_topics(self) -> None:
        bridge, _ = _make_bridge()

        out = bridge.desired_state(Snapshot(failed={"settings"}))

        assert f"{PREFIX}/system/enabled/state" == next(
            t for t in out if t.startswith(f"{PREFIX}/system/")
        )
        assert not any("vacation_mode" in t for t in out)


# ---------------------------------------------------------------------------
# Command resolution failures
# ---------------------------------------------------------------------------


class TestCommandResolution:
    async def test_an_unknown_system_control_is_reported(self) -> None:
        bridge, _, transport = _connected()

        await bridge.handle_message(f"{PREFIX}/system/not_a_control/set", "ON")

        result = transport.results()[f"{PREFIX}/system/not_a_control/set/result"]
        assert result["ok"] is False
        assert "unknown system control" in result["error"]

    async def test_an_unknown_thermostat_is_reported(self) -> None:
        bridge, api, transport = _connected(FakeApi(thermostats=[{"thermostat_entity_id": THERMO}]))

        await bridge.handle_message(f"{PREFIX}/thermostat/climate_basement/deadband/set", "1")

        result = transport.results()[f"{PREFIX}/thermostat/climate_basement/deadband/set/result"]
        assert result["ok"] is False
        assert "unknown thermostat 'climate_basement'" in result["error"]
        assert api.writes == []

    async def test_an_unknown_thermostat_control_is_reported(self) -> None:
        bridge, api, transport = _connected(FakeApi(thermostats=[{"thermostat_entity_id": THERMO}]))

        await bridge.handle_message(f"{PREFIX}/thermostat/{THERMO_IDENT}/not_a_control/set", "1")

        result = transport.results()[f"{PREFIX}/thermostat/{THERMO_IDENT}/not_a_control/set/result"]
        assert result["ok"] is False
        assert "unknown thermostat control" in result["error"]
        assert api.writes == []

    async def test_a_result_is_dropped_rather_than_crashing_when_the_broker_is_gone(self) -> None:
        """A command can be in flight when the connection drops; there is
        nowhere to publish the verdict, and that must not raise."""
        bridge, _ = _make_bridge()
        assert bridge._transport is None

        await bridge.handle_message(f"{PREFIX}/system/not_a_control/set", "ON")

        # ...and with a transport, the very same command does produce a result,
        # so the no-op above is the missing transport and nothing else.
        transport = FakeTransport()
        bridge._transport = transport
        await bridge.handle_message(f"{PREFIX}/system/not_a_control/set", "ON")
        assert transport.results()[f"{PREFIX}/system/not_a_control/set/result"]["ok"] is False

    async def test_an_error_response_without_a_message_falls_back_to_the_status(self) -> None:
        """Route handlers return sanitised `{"error": ...}` bodies, but a
        proxy/timeout answer may not. The result topic still has to say
        something, and it must never be an empty error."""
        api = FakeApi(rooms=[{"id": "r1", "name": "Office"}])
        bridge, api, transport = _connected(api)
        api.write_result = (503, None)

        await bridge.handle_message(f"{PREFIX}/room/r1/temp_offset/set", "2")

        result = transport.results()[f"{PREFIX}/room/r1/temp_offset/set/result"]
        assert result == {"ok": False, "error": "HTTP 503"}

    async def test_a_structured_error_body_is_echoed_verbatim(self) -> None:
        api = FakeApi(rooms=[{"id": "r1", "name": "Office"}])
        bridge, api, transport = _connected(api)
        api.write_result = (400, {"error": "temp_offset must be between -20 and 20"})

        await bridge.handle_message(f"{PREFIX}/room/r1/temp_offset/set", "99")

        result = transport.results()[f"{PREFIX}/room/r1/temp_offset/set/result"]
        assert result["error"] == "temp_offset must be between -20 and 20"


# ---------------------------------------------------------------------------
# commands.py decoding edges
# ---------------------------------------------------------------------------


class TestDecodeValueKinds:
    def test_bool(self) -> None:
        control = control_for(DEVICE_ROOM, "include_thermostat_sensor")
        assert control is not None
        assert decode_value(control, "ON") is True
        assert decode_value(control, "off") is False

    def test_datetime_is_normalised_to_an_iso_utc_string(self) -> None:
        control = control_for(DEVICE_SYSTEM, "vacation_mode/return_at")
        assert control is not None
        assert decode_value(control, "2026-08-01T12:00:00-04:00") == "2026-08-01T16:00:00+00:00"

    def test_an_action_carries_no_value(self) -> None:
        """A button press has no payload to decode — the press IS the command."""
        control = control_for(DEVICE_ROOM, "presence")
        assert control is not None
        assert decode_value(control, "PRESS") is None

    def test_an_unknown_kind_is_rejected_rather_than_silently_dropped(self) -> None:
        control = Control(key="x", entity="switch", name="X", kind="frobnicate", field="x")
        with pytest.raises(CommandError, match="unsupported control kind 'frobnicate'"):
            decode_value(control, "ON")


class TestBuildRequestEdges:
    def test_an_unmapped_special_is_rejected(self) -> None:
        """`special` names an endpoint in `_build_special`. A control that adds
        one without the matching branch must fail loudly, not write nothing."""
        control = Control(
            key="new_thing", entity="switch", name="New", kind=KIND_BOOL, special="not_implemented"
        )
        with pytest.raises(CommandError, match="unsupported control 'new_thing'"):
            build_request(control, "set", "ON", device=DEVICE_SYSTEM, resolved_id="")

    def test_an_unknown_verb_is_treated_as_a_set(self) -> None:
        assert commands.verb_of("frobnicate") == "set"
        assert commands.verb_of("clear") == "clear"


# ---------------------------------------------------------------------------
# registry.py / topics.py leftovers
# ---------------------------------------------------------------------------


class TestControlVerbs:
    def test_a_plain_required_field_offers_only_set(self) -> None:
        """No `.../clear` topic and no clear button in HA: clearing a required
        field has no meaning, and `build_request` rejects it."""
        control = control_for(DEVICE_ROOM, "temp_offset")
        assert control is not None
        assert not control.nullable and not control.clearable
        assert control.verbs == ("set",)

    def test_a_nullable_field_adds_clear(self) -> None:
        control = control_for(DEVICE_ROOM, "deadband_override")
        assert control is not None
        assert control.verbs == ("set", "clear")


class TestSupervisorDiscoveryEdges:
    def test_a_supervisor_service_without_a_host_leaves_the_bridge_unconfigured(
        self, monkeypatch
    ) -> None:
        """The Supervisor answers with a service entry even when no broker
        add-on is actually installed. Announcing "discovered" off that would
        promise a connection that can never happen — and the bridge must stay
        unavailable rather than dialling an empty hostname."""
        for name in (
            "MQTT_HOST",
            "MQTT_PORT",
            "MQTT_USER",
            "MQTT_PASSWORD",
            "MQTT_TOPIC_PREFIX",
            "ADDON_SLUG",
        ):
            monkeypatch.delenv(name, raising=False)

        config = load_config(
            supervisor_lookup=lambda: {"port": 1885, "username": "svc"},
            slug_lookup=lambda: "should_not_be_asked",
        )

        assert config.host == ""
        assert config.configured is False
        # The rest of the service entry is still honoured for when a host does
        # arrive from somewhere else.
        assert config.port == 1885
        assert config.username == "svc"
        assert config.prefix_is_fallback is True


class TestTopicRoots:
    def test_room_topic_root_is_the_whole_alias_subtree(self) -> None:
        assert topics.room_topic_root(PREFIX, "living_room") == f"{PREFIX}/room/living_room"

    def test_a_prefix_with_stray_slashes_is_normalised(self) -> None:
        """The resolved prefix comes from the add-on slug; a stray slash would
        otherwise produce a double separator and a topic nothing subscribes to."""
        assert topics.room_topic_root("/plenum/", "r1") == f"{PREFIX}/room/r1"

    def test_the_root_is_the_prefix_of_every_topic_in_that_room(self) -> None:
        root = topics.room_topic_root(PREFIX, "r1")
        assert topics.state_topic(PREFIX, "room", "r1", "hold").startswith(root + "/")
        assert topics.command_topic(PREFIX, "room", "r1", "hold", "set").startswith(root + "/")
