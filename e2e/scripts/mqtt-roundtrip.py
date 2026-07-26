#!/usr/bin/env python3
"""
End-to-end MQTT round-trip against a real broker (Issue #519).

The backend suite covers the bridge's logic against a fake transport; this
covers the parts only a real broker can show:

  * Plenum actually connects, subscribes, and publishes.
  * Retained state survives and is readable by a fresh subscriber — which is how
    Home Assistant sees it after a restart.
  * A published command reaches the REST write boundary and changes the DB.
  * A rejected command reports on its result topic instead of silently
    no-op'ing (#519's driving requirement).
  * HA MQTT Discovery configs land under the discovery prefix.
  * Renaming a room retires its old name-alias topics.
  * The runtime toggle really gates the connection.

Deliberately not a Playwright spec: there is no browser involved, so putting it
in the Playwright suite would cost a browser launch for nothing and force a
grep-invert in three other legs.

Usage:
    python3 mqtt-roundtrip.py --plenum-url http://localhost:8099 \
        --broker localhost --port 1883 --prefix plenum
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time

import paho.mqtt.client as mqtt
import requests

TIMEOUT = 30


class Collector:
    """Records every message the broker delivers, newest value per topic."""

    def __init__(self) -> None:
        self.messages: dict[str, str] = {}
        self.arrivals: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._event = threading.Event()

    def on_message(self, _client, _userdata, msg) -> None:
        payload = msg.payload.decode("utf-8", "replace")
        with self._lock:
            self.messages[msg.topic] = payload
            self.arrivals.append((msg.topic, payload))
        self._event.set()

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self.messages)

    def count(self, topic: str) -> int:
        """How many messages have arrived on *topic* so far.

        Result topics are reused, so "did a new result arrive?" cannot be
        answered by looking at the latest value — a second failure after a
        success would look identical to the success still sitting there.
        """
        with self._lock:
            return sum(1 for t, _ in self.arrivals if t == topic)

    def wait_for_new(self, topic: str, since: int, what: str, timeout: int = TIMEOUT) -> str:
        """Wait for a message on *topic* beyond the *since*'th one."""
        self.wait_for(lambda _m: self.count(topic) > since, what, timeout)
        return self.snapshot()[topic]

    def wait_for(self, predicate, what: str, timeout: int = TIMEOUT):
        """Block until *predicate* is satisfied by the collected messages."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = predicate(self.snapshot())
            if result:
                return result
            self._event.wait(0.25)
            self._event.clear()
        fail(f"timed out after {timeout}s waiting for {what}")


def fail(message: str) -> None:
    print(f"\n❌ {message}", flush=True)
    sys.exit(1)


def ok(message: str) -> None:
    print(f"✅ {message}", flush=True)


def api(method: str, url: str, **kwargs) -> requests.Response:
    resp = requests.request(method, url, timeout=15, **kwargs)
    if resp.status_code >= 400:
        fail(f"{method} {url} → {resp.status_code}: {resp.text}")
    return resp


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plenum-url", default="http://localhost:8099")
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--prefix", default="plenum")
    parser.add_argument("--discovery-prefix", default="homeassistant")
    args = parser.parse_args()

    base = args.plenum_url.rstrip("/")
    prefix = args.prefix

    # ------------------------------------------------------------------
    # 1. The runtime toggle gates the connection.
    # ------------------------------------------------------------------
    status = api("GET", f"{base}/api/settings/mqtt").json()
    if not status["configured"]:
        fail(f"Plenum reports no broker configured: {status}")
    if status["connected"]:
        fail("the bridge is connected before the toggle was turned on")
    ok("bridge is configured but not connected while the toggle is off")

    api("POST", f"{base}/api/system/mqtt", json={"mqtt_enabled": True})

    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        if api("GET", f"{base}/api/settings/mqtt").json()["connected"]:
            break
        time.sleep(1)
    else:
        fail("bridge never connected after the toggle was turned on")
    ok("bridge connected after the toggle was turned on")

    # ------------------------------------------------------------------
    # 2. A room to drive, created through REST.
    # ------------------------------------------------------------------
    room = api(
        "POST",
        f"{base}/api/rooms",
        json={"name": "Mqtt Test Room", "thermostat_entity_id": "climate.test_thermostat"},
    ).json()
    room_id = room["id"]
    ok(f"created room {room_id}")

    # ------------------------------------------------------------------
    # 3. Subscribe as a fresh client — this is what HA does on restart, so
    #    everything below has to arrive as *retained* state, not as a live
    #    push we happened to be listening for.
    # ------------------------------------------------------------------
    collector = Collector()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_message = collector.on_message
    client.connect(args.broker, args.port, keepalive=30)
    client.subscribe([(f"{prefix}/#", 1), (f"{args.discovery_prefix}/#", 1)])
    client.loop_start()

    try:
        collector.wait_for(
            lambda m: m.get(f"{prefix}/status") == "online",
            "the availability topic to read 'online'",
        )
        ok("availability topic is retained and reads 'online'")

        state_topic = f"{prefix}/room/{room_id}/temp_offset/state"
        collector.wait_for(lambda m: state_topic in m, f"retained state on {state_topic}")
        ok("room state is published under the room id")

        name_topic = f"{prefix}/room/mqtt_test_room/temp_offset/state"
        collector.wait_for(lambda m: name_topic in m, f"retained state on {name_topic}")
        ok("room state is mirrored under the sanitized room name")

        # --------------------------------------------------------------
        # 4. A command by id changes the DB.
        # --------------------------------------------------------------
        client.publish(f"{prefix}/room/{room_id}/temp_offset/set", "1.5", qos=1)
        result = collector.wait_for(
            lambda m: m.get(f"{prefix}/room/{room_id}/temp_offset/set/result"),
            "a result on the id-addressed set topic",
        )
        if json.loads(result) != {"ok": True}:
            fail(f"expected a success result, got {result}")
        stored = api("GET", f"{base}/api/rooms/{room_id}").json()["temp_offset"]
        if stored != 1.5:
            fail(f"temp_offset should be 1.5 after the MQTT command, got {stored}")
        ok("command addressed by room id reached the database")

        collector.wait_for(
            lambda m: m.get(state_topic) == "1.5",
            "the state topic to reflect the new value",
        )
        ok("state topic was republished with the new value")

        # --------------------------------------------------------------
        # 5. A command by name works too, and its result echoes back on the
        #    name segment the command used.
        # --------------------------------------------------------------
        client.publish(f"{prefix}/room/mqtt_test_room/temp_offset/set", "2.5", qos=1)
        result = collector.wait_for(
            lambda m: m.get(f"{prefix}/room/mqtt_test_room/temp_offset/set/result"),
            "a result on the name-addressed set topic",
        )
        if json.loads(result) != {"ok": True}:
            fail(f"expected a success result, got {result}")
        stored = api("GET", f"{base}/api/rooms/{room_id}").json()["temp_offset"]
        if stored != 2.5:
            fail(f"temp_offset should be 2.5 after the name-addressed command, got {stored}")
        ok("command addressed by room name reached the database")

        # --------------------------------------------------------------
        # 6. A rejected command must SAY so. This is the case #519 was
        #    written around: silent no-ops are the failure mode.
        # --------------------------------------------------------------
        client.publish(f"{prefix}/room/{room_id}/system_wide_temp/set", "999", qos=1)
        result = collector.wait_for(
            lambda m: m.get(f"{prefix}/room/{room_id}/system_wide_temp/set/result"),
            "a result for the out-of-range command",
        )
        parsed = json.loads(result)
        if parsed.get("ok") is not False or not parsed.get("error"):
            fail(f"an out-of-range value should be rejected with an error, got {result}")
        ok(f"rejected command reported on its result topic: {parsed['error']!r}")

        offset_result = f"{prefix}/room/{room_id}/temp_offset/set/result"
        seen = collector.count(offset_result)
        client.publish(f"{prefix}/room/{room_id}/temp_offset/set", "banana", qos=1)
        result = collector.wait_for_new(
            offset_result, seen, "a failure result for the unparseable payload"
        )
        parsed = json.loads(result)
        if parsed.get("ok") is not False or not parsed.get("error"):
            fail(f"an unparseable payload should be rejected with an error, got {result}")
        stored = api("GET", f"{base}/api/rooms/{room_id}").json()["temp_offset"]
        if stored != 2.5:
            fail(f"a rejected command must not change anything; temp_offset is now {stored}")
        ok("unparseable payload reported on its result topic and changed nothing")

        # --------------------------------------------------------------
        # 7. HA Discovery configs exist and are id-based.
        # --------------------------------------------------------------
        configs = {
            topic: payload
            for topic, payload in collector.snapshot().items()
            if topic.startswith(f"{args.discovery_prefix}/") and payload
        }
        if not configs:
            fail("no HA discovery configs were published")
        hold = [
            json.loads(p)
            for p in configs.values()
            if json.loads(p).get("command_topic", "").endswith(f"/room/{room_id}/hold/set")
        ]
        if not hold:
            fail("no discovery config found for the room's hold control")
        if "mqtt_test_room" in json.dumps(hold[0]):
            fail("a discovery payload referenced the room NAME; it must be id-based only")
        ok(f"{len(configs)} HA discovery configs published, all id-addressed")

        # --------------------------------------------------------------
        # 8. Renaming retires the old name alias rather than leaving ghosts.
        # --------------------------------------------------------------
        api("PUT", f"{base}/api/rooms/{room_id}", json={"name": "Mqtt Renamed Room"})
        collector.wait_for(
            lambda m: m.get(name_topic) == "",
            "the old name-alias topic to be blanked",
        )
        collector.wait_for(
            lambda m: f"{prefix}/room/mqtt_renamed_room/temp_offset/state" in m,
            "state under the new name alias",
        )
        if collector.snapshot().get(state_topic) in (None, ""):
            fail("the id-addressed topic must survive a rename untouched")
        ok("rename retired the old name alias and published the new one")

        # --------------------------------------------------------------
        # 9. Turning the toggle off disconnects and marks us offline.
        # --------------------------------------------------------------
        api("POST", f"{base}/api/system/mqtt", json={"mqtt_enabled": False})
        collector.wait_for(
            lambda m: m.get(f"{prefix}/status") == "offline",
            "the availability topic to read 'offline'",
        )
        ok("turning the toggle off disconnected the bridge")

    finally:
        client.loop_stop()
        client.disconnect()
        # Leave the instance as we found it.
        requests.delete(f"{base}/api/rooms/{room_id}", timeout=15)

    print("\n🎉 MQTT round-trip passed", flush=True)


if __name__ == "__main__":
    main()
