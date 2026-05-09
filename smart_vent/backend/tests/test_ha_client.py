"""
Unit tests for HAClient and build_ha_client.

These tests exercise the real HAClient class (not FakeHomeAssistant) to
cover the ha_client.py module which has <20% coverage from integration tests
alone (they use FakeHA and never touch the real WS client).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ha_client import HAClient, build_ha_client

# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestHAClientInit:
    def test_default_init(self):
        client = HAClient("http://ha.local:8123", "tok")
        assert client._ha_url == "http://ha.local:8123"
        assert client._token == "tok"
        assert client._ssl_verify is True
        assert not client._running
        assert not client.dev_mode
        assert client._dev_logger is None
        assert client._ws is None
        assert client._session is None
        assert client._msg_id == 0
        assert client._pending == {}
        assert client._sub_id is None

    def test_ssl_verify_false(self):
        client = HAClient("http://ha.local", "tok", ssl_verify=False)
        assert client._ssl_verify is False

    def test_trailing_slash_stripped(self):
        client = HAClient("http://ha.local:8123/", "tok")
        assert client._ha_url == "http://ha.local:8123"


# ---------------------------------------------------------------------------
# State cache
# ---------------------------------------------------------------------------


class TestStateCache:
    def setup_method(self):
        self.client = HAClient("http://ha.local", "tok")
        self.client._state_cache = {
            "sensor.temp_f": {
                "state": "72.5",
                "attributes": {"unit_of_measurement": "°F"},
            },
            "sensor.temp_c": {
                "state": "22.0",
                "attributes": {"unit_of_measurement": "°C"},
            },
            "binary_sensor.door": {"state": "on", "attributes": {}},
            "sensor.bad": {"state": "unavailable", "attributes": {}},
        }

    def test_get_state_present(self):
        s = self.client.get_state("sensor.temp_f")
        assert s["state"] == "72.5"

    def test_get_state_missing(self):
        assert self.client.get_state("non.existent") is None

    def test_get_state_attr_present(self):
        u = self.client.get_state_attr("sensor.temp_f", "unit_of_measurement")
        assert u == "°F"

    def test_get_state_attr_default_when_missing_key(self):
        v = self.client.get_state_attr("sensor.temp_f", "nonexistent_key", "fallback")
        assert v == "fallback"

    def test_get_state_attr_default_when_missing_entity(self):
        v = self.client.get_state_attr("missing.entity", "anything", 42)
        assert v == 42

    def test_get_numeric_state_fahrenheit(self):
        v = self.client.get_numeric_state("sensor.temp_f")
        assert v == 72.5

    def test_get_numeric_state_celsius_converted(self):
        v = self.client.get_numeric_state("sensor.temp_c")
        expected = 22.0 * 9 / 5 + 32
        assert abs(v - expected) < 0.001

    def test_get_numeric_state_non_numeric_returns_none(self):
        assert self.client.get_numeric_state("binary_sensor.door") is None

    def test_get_numeric_state_unavailable_returns_none(self):
        assert self.client.get_numeric_state("sensor.bad") is None

    def test_get_numeric_state_missing_entity(self):
        assert self.client.get_numeric_state("nope.entity") is None


# ---------------------------------------------------------------------------
# Subscribe
# ---------------------------------------------------------------------------


class TestSubscribe:
    def setup_method(self):
        self.client = HAClient("http://ha.local", "tok")

    async def _cb(self, eid, state):
        pass

    def test_subscribe_entity(self):
        self.client.subscribe("climate.main", self._cb)
        assert self._cb in self.client._listeners["climate.main"]

    def test_subscribe_all(self):
        self.client.subscribe_all(self._cb)
        assert self._cb in self.client._wildcard_listeners

    def test_subscribe_multiple_same_entity(self):
        async def cb2(eid, state):
            pass

        self.client.subscribe("climate.main", self._cb)
        self.client.subscribe("climate.main", cb2)
        assert len(self.client._listeners["climate.main"]) == 2


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


class TestStop:
    async def test_stop_clears_running_flag(self):
        client = HAClient("http://ha.local", "tok")
        client._running = True
        mock_session = AsyncMock()
        client._session = mock_session
        await client.stop()
        assert not client._running

    async def test_stop_closes_open_ws(self):
        client = HAClient("http://ha.local", "tok")
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_session = AsyncMock()
        client._ws = mock_ws
        client._session = mock_session
        await client.stop()
        mock_ws.close.assert_called_once()
        mock_session.close.assert_called_once()

    async def test_stop_skips_already_closed_ws(self):
        client = HAClient("http://ha.local", "tok")
        mock_ws = MagicMock()
        mock_ws.closed = True
        mock_session = AsyncMock()
        client._ws = mock_ws
        client._session = mock_session
        await client.stop()
        mock_ws.close.assert_not_called()
        mock_session.close.assert_called_once()

    async def test_stop_without_ws_or_session(self):
        client = HAClient("http://ha.local", "tok")
        await client.stop()  # should not raise


# ---------------------------------------------------------------------------
# wait_connected()
# ---------------------------------------------------------------------------


class TestWaitConnected:
    async def test_wait_connected_when_already_set(self):
        client = HAClient("http://ha.local", "tok")
        client._connected.set()
        await asyncio.wait_for(client.wait_connected(timeout=1.0), timeout=2.0)

    async def test_wait_connected_times_out(self):
        client = HAClient("http://ha.local", "tok")
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await client.wait_connected(timeout=0.01)


# ---------------------------------------------------------------------------
# get_entities_by_domain()
# ---------------------------------------------------------------------------


class TestGetEntitiesByDomain:
    async def test_filters_by_domain(self):
        client = HAClient("http://ha.local", "tok")
        client._state_cache = {
            "climate.main": {"entity_id": "climate.main", "state": "heat"},
            "climate.upstairs": {"entity_id": "climate.upstairs", "state": "cool"},
            "sensor.temp": {"entity_id": "sensor.temp", "state": "72"},
        }
        client._connected.set()
        result = await client.get_entities_by_domain("climate")
        entity_ids = {e["entity_id"] for e in result}
        assert "climate.main" in entity_ids
        assert "climate.upstairs" in entity_ids
        assert "sensor.temp" not in entity_ids

    async def test_empty_when_no_match(self):
        client = HAClient("http://ha.local", "tok")
        client._state_cache = {"sensor.temp": {"entity_id": "sensor.temp", "state": "72"}}
        client._connected.set()
        result = await client.get_entities_by_domain("cover")
        assert result == []


# ---------------------------------------------------------------------------
# dev_mode paths for cover / thermostat methods
# ---------------------------------------------------------------------------


class TestDevModeNoop:
    """Verify dev_mode=True methods short-circuit without calling HA."""

    def setup_method(self):
        self.client = HAClient("ws://ha.local", "tok")
        self.client.dev_mode = True

    async def test_set_thermostat_dev_no_logger(self):
        await self.client.set_thermostat_temperature("climate.main", 72.0)
        # no _ws set, would blow up if call_service were reached

    async def test_set_thermostat_dev_with_logger_and_hvac_mode(self):
        mock_logger = AsyncMock()
        self.client._dev_logger = mock_logger
        await self.client.set_thermostat_temperature("climate.main", 70.0, hvac_mode="heat")
        mock_logger.log.assert_called_once()
        args = mock_logger.log.call_args[0]
        assert "heat" in args[3].get("hvac_mode", "")

    async def test_set_thermostat_dev_with_logger_no_hvac_mode(self):
        mock_logger = AsyncMock()
        self.client._dev_logger = mock_logger
        await self.client.set_thermostat_temperature("climate.main", 70.0)
        mock_logger.log.assert_called_once()

    async def test_open_cover_dev_no_logger(self):
        await self.client.open_cover("cover.vent1")

    async def test_open_cover_dev_with_logger(self):
        mock_logger = AsyncMock()
        self.client._dev_logger = mock_logger
        await self.client.open_cover("cover.vent1")
        mock_logger.log.assert_called_once()

    async def test_close_cover_dev_no_logger(self):
        await self.client.close_cover("cover.vent1")

    async def test_close_cover_dev_with_logger(self):
        mock_logger = AsyncMock()
        self.client._dev_logger = mock_logger
        await self.client.close_cover("cover.vent1")
        mock_logger.log.assert_called_once()

    async def test_set_cover_position_dev_no_logger(self):
        await self.client.set_cover_position("cover.vent1", 50)

    async def test_set_cover_position_dev_with_logger(self):
        mock_logger = AsyncMock()
        self.client._dev_logger = mock_logger
        await self.client.set_cover_position("cover.vent1", 100)
        mock_logger.log.assert_called_once()

    async def test_set_cover_tilt_dev_no_logger(self):
        await self.client.set_cover_tilt_position("cover.vent1", 75)

    async def test_set_cover_tilt_dev_with_logger(self):
        mock_logger = AsyncMock()
        self.client._dev_logger = mock_logger
        await self.client.set_cover_tilt_position("cover.vent1", 0)
        mock_logger.log.assert_called_once()

    async def test_toggle_cover_dev_no_logger(self):
        await self.client.toggle_cover("cover.vent1")

    async def test_toggle_cover_dev_with_logger(self):
        mock_logger = AsyncMock()
        self.client._dev_logger = mock_logger
        await self.client.toggle_cover("cover.vent1")
        mock_logger.log.assert_called_once()


# ---------------------------------------------------------------------------
# Non-dev-mode paths (via _send mock)
# ---------------------------------------------------------------------------


def _make_auto_resolving_client() -> HAClient:
    """Return an HAClient whose mock _ws auto-resolves any pending future."""
    client = HAClient("ws://ha.local", "tok")

    async def fake_send_json(msg):
        fut = client._pending.get(msg["id"])
        if fut and not fut.done():
            fut.set_result({})

    mock_ws = AsyncMock()
    mock_ws.send_json.side_effect = fake_send_json
    client._ws = mock_ws
    client._connected.set()
    return client


class TestNonDevMode:
    async def test_open_cover_calls_service(self):
        client = _make_auto_resolving_client()
        await client.open_cover("cover.vent1")
        client._ws.send_json.assert_called_once()
        payload = client._ws.send_json.call_args[0][0]
        assert payload["service"] == "open_cover"

    async def test_close_cover_calls_service(self):
        client = _make_auto_resolving_client()
        await client.close_cover("cover.vent1")
        payload = client._ws.send_json.call_args[0][0]
        assert payload["service"] == "close_cover"

    async def test_set_cover_position_calls_service(self):
        client = _make_auto_resolving_client()
        await client.set_cover_position("cover.vent1", 50)
        payload = client._ws.send_json.call_args[0][0]
        assert payload["service"] == "set_cover_position"

    async def test_set_cover_tilt_calls_service(self):
        client = _make_auto_resolving_client()
        await client.set_cover_tilt_position("cover.vent1", 80)
        payload = client._ws.send_json.call_args[0][0]
        assert payload["service"] == "set_cover_tilt_position"

    async def test_toggle_cover_calls_service(self):
        client = _make_auto_resolving_client()
        await client.toggle_cover("cover.vent1")
        payload = client._ws.send_json.call_args[0][0]
        assert payload["service"] == "toggle"

    async def test_set_thermostat_no_hvac_mode(self):
        client = _make_auto_resolving_client()
        await client.set_thermostat_temperature("climate.main", 72.0)
        payload = client._ws.send_json.call_args[0][0]
        assert payload["service"] == "set_temperature"
        assert "hvac_mode" not in payload["service_data"]

    async def test_set_thermostat_with_hvac_mode(self):
        client = _make_auto_resolving_client()
        await client.set_thermostat_temperature("climate.main", 72.0, hvac_mode="cool")
        payload = client._ws.send_json.call_args[0][0]
        assert payload["service_data"]["hvac_mode"] == "cool"

    async def test_call_service_returns_result(self):
        client = _make_auto_resolving_client()
        result = await client.call_service("cover", "open_cover", {"entity_id": "cover.v1"})
        assert result == {}


# ---------------------------------------------------------------------------
# fetch_states()
# ---------------------------------------------------------------------------


class TestFetchStates:
    async def test_fetch_states_populates_cache(self):
        client = HAClient("http://ha.local:8123", "tok")
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(
            return_value=[
                {"entity_id": "climate.main", "state": "cool"},
                {"entity_id": "sensor.temp", "state": "70"},
            ]
        )
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session

        states = await client.fetch_states()
        assert len(states) == 2
        assert client._state_cache["climate.main"]["state"] == "cool"
        assert client._state_cache["sensor.temp"]["state"] == "70"


# ---------------------------------------------------------------------------
# _dispatch()
# ---------------------------------------------------------------------------


class TestDispatch:
    def setup_method(self):
        self.client = HAClient("http://ha.local", "tok")

    async def test_dispatch_result_success_resolves_future(self):
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self.client._pending[1] = fut
        await self.client._dispatch(
            {"type": "result", "id": 1, "success": True, "result": {"ok": True}}
        )
        assert fut.done()
        assert fut.result() == {"ok": True}

    async def test_dispatch_result_failure_sets_exception(self):
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self.client._pending[2] = fut
        await self.client._dispatch(
            {"type": "result", "id": 2, "success": False, "error": "auth failed"}
        )
        assert fut.done()
        assert fut.exception() is not None

    async def test_dispatch_result_unknown_id_noop(self):
        # Shouldn't raise even if id isn't in pending
        await self.client._dispatch({"type": "result", "id": 999, "success": True})

    async def test_dispatch_state_changed_updates_cache(self):
        await self.client._dispatch(
            {
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {
                        "entity_id": "sensor.temp",
                        "new_state": {"state": "75.0", "attributes": {}},
                    },
                },
            }
        )
        assert self.client._state_cache["sensor.temp"]["state"] == "75.0"

    async def test_dispatch_state_changed_fires_entity_listener(self):
        received = []

        async def cb(eid, state):
            received.append((eid, state))

        self.client.subscribe("sensor.temp", cb)
        await self.client._dispatch(
            {
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {
                        "entity_id": "sensor.temp",
                        "new_state": {"state": "77.0", "attributes": {}},
                    },
                },
            }
        )
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0][0] == "sensor.temp"

    async def test_dispatch_state_changed_fires_wildcard_listener(self):
        received = []

        async def cb(eid, state):
            received.append(eid)

        self.client.subscribe_all(cb)
        await self.client._dispatch(
            {
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {
                        "entity_id": "climate.main",
                        "new_state": {"state": "heat", "attributes": {}},
                    },
                },
            }
        )
        await asyncio.sleep(0.05)
        assert "climate.main" in received

    async def test_dispatch_state_changed_null_new_state(self):
        # new_state=None should not crash and should not update cache
        await self.client._dispatch(
            {
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {"entity_id": "sensor.x", "new_state": None},
                },
            }
        )
        # No entry added because new_state is falsy
        assert "sensor.x" not in self.client._state_cache

    async def test_dispatch_unknown_type_is_noop(self):
        await self.client._dispatch({"type": "pong"})  # must not raise


# ---------------------------------------------------------------------------
# build_ha_client()
# ---------------------------------------------------------------------------


class TestBuildHaClient:
    def test_default_fallback_url(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_TOKEN", raising=False)
        monkeypatch.delenv("HA_URL", raising=False)
        monkeypatch.delenv("HA_USE_WSS", raising=False)
        monkeypatch.delenv("HA_SSL_VERIFY", raising=False)
        client = build_ha_client()
        assert "homeassistant.local" in client._ha_url

    def test_env_url_and_token(self, monkeypatch):
        monkeypatch.setenv("HA_URL", "http://myha.local:8123")
        monkeypatch.setenv("HA_TOKEN", "mytoken")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_USE_WSS", raising=False)
        monkeypatch.delenv("HA_SSL_VERIFY", raising=False)
        client = build_ha_client()
        assert "myha.local" in client._ha_url
        assert client._token == "mytoken"

    def test_supervisor_token_fallback_url(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "sup_tok_xyz")
        monkeypatch.delenv("HA_URL", raising=False)
        monkeypatch.delenv("HA_TOKEN", raising=False)
        monkeypatch.delenv("HA_USE_WSS", raising=False)
        monkeypatch.delenv("HA_SSL_VERIFY", raising=False)
        client = build_ha_client()
        assert "supervisor" in client._ha_url
        assert client._token == "sup_tok_xyz"

    def test_supervisor_proxy_url_uses_ws(self, monkeypatch):
        monkeypatch.setenv("HA_URL", "http://supervisor/core")
        monkeypatch.setenv("HA_TOKEN", "tok")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_USE_WSS", raising=False)
        monkeypatch.delenv("HA_SSL_VERIFY", raising=False)
        client = build_ha_client()
        assert client._ha_url.startswith("ws://supervisor")

    def test_use_wss_forces_https_scheme(self, monkeypatch):
        # use_wss converts http→https in the stored URL; _connect converts to wss at runtime
        monkeypatch.setenv("HA_URL", "http://myha.local:8123")
        monkeypatch.setenv("HA_TOKEN", "tok")
        monkeypatch.setenv("HA_USE_WSS", "true")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_SSL_VERIFY", raising=False)
        client = build_ha_client()
        assert "myha.local" in client._ha_url
        assert not client._ha_url.startswith("ws://")

    def test_ssl_verify_disabled(self, monkeypatch):
        monkeypatch.setenv("HA_URL", "http://myha.local:8123")
        monkeypatch.setenv("HA_TOKEN", "tok")
        monkeypatch.setenv("HA_SSL_VERIFY", "false")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_USE_WSS", raising=False)
        client = build_ha_client()
        assert not client._ssl_verify

    def test_ssl_verify_zero(self, monkeypatch):
        monkeypatch.setenv("HA_URL", "http://myha.local:8123")
        monkeypatch.setenv("HA_TOKEN", "tok")
        monkeypatch.setenv("HA_SSL_VERIFY", "0")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_USE_WSS", raising=False)
        client = build_ha_client()
        assert not client._ssl_verify

    def test_ssl_verify_default_true(self, monkeypatch):
        monkeypatch.setenv("HA_URL", "http://myha.local:8123")
        monkeypatch.setenv("HA_TOKEN", "tok")
        monkeypatch.delenv("HA_SSL_VERIFY", raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_USE_WSS", raising=False)
        client = build_ha_client()
        assert client._ssl_verify

    def test_https_url_becomes_wss(self, monkeypatch):
        monkeypatch.setenv("HA_URL", "https://myha.local:8123")
        monkeypatch.setenv("HA_TOKEN", "tok")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_USE_WSS", raising=False)
        monkeypatch.delenv("HA_SSL_VERIFY", raising=False)
        client = build_ha_client()
        assert client._ha_url.startswith("wss://")

    def test_use_wss_yes_variant(self, monkeypatch):
        monkeypatch.setenv("HA_URL", "http://myha.local:8123")
        monkeypatch.setenv("HA_TOKEN", "tok")
        monkeypatch.setenv("HA_USE_WSS", "yes")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_SSL_VERIFY", raising=False)
        client = build_ha_client()
        assert "myha.local" in client._ha_url
        assert not client._ha_url.startswith("ws://")

    def test_no_token_empty_string(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_TOKEN", raising=False)
        monkeypatch.setenv("HA_URL", "http://myha.local")
        monkeypatch.delenv("HA_USE_WSS", raising=False)
        monkeypatch.delenv("HA_SSL_VERIFY", raising=False)
        client = build_ha_client()
        assert client._token == ""
