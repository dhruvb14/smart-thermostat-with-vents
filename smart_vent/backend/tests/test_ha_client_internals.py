"""
Tests for HAClient internal connection methods (_handshake, _subscribe_state_changed,
_read_loop) and the start() retry loop. These target ha_client.py lines that are
only exercised by the real WebSocket lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from backend.ha_client import HAClient

# ---------------------------------------------------------------------------
# _handshake()
# ---------------------------------------------------------------------------


class TestHandshake:
    async def test_successful_handshake(self):
        client = HAClient("ws://ha.local", "tok")
        mock_ws = AsyncMock()
        mock_ws.receive_json = AsyncMock(
            side_effect=[
                {"type": "auth_required"},
                {"type": "auth_ok", "ha_version": "2024.1.0"},
            ]
        )
        mock_ws.send_json = AsyncMock()
        client._ws = mock_ws
        await client._handshake()
        mock_ws.send_json.assert_called_once_with({"type": "auth", "access_token": "tok"})

    async def test_auth_failure_raises(self):
        client = HAClient("ws://ha.local", "tok")
        mock_ws = AsyncMock()
        mock_ws.receive_json = AsyncMock(
            side_effect=[
                {"type": "auth_required"},
                {"type": "auth_invalid", "message": "bad token"},
            ]
        )
        mock_ws.send_json = AsyncMock()
        client._ws = mock_ws
        with pytest.raises(ValueError, match="auth failed"):
            await client._handshake()

    async def test_unexpected_first_message_raises(self):
        """A peer that skips `auth_required` (non-HA endpoint, broken proxy)
        must raise so start() reconnects — not proceed with the handshake."""
        client = HAClient("ws://ha.local", "tok")
        mock_ws = AsyncMock()
        mock_ws.receive_json = AsyncMock(return_value={"type": "pong"})
        mock_ws.send_json = AsyncMock()
        client._ws = mock_ws
        with pytest.raises(ValueError, match="expected auth_required"):
            await client._handshake()
        # The handshake must abort before ever sending credentials.
        mock_ws.send_json.assert_not_called()

    async def test_handshake_times_out_on_silent_peer(self):
        """Issue #297: a peer that accepts the upgrade but never sends
        `auth_required` must not block forever — the receive is timeout-guarded."""
        client = HAClient("ws://ha.local", "tok")
        mock_ws = AsyncMock()

        async def slow_receive():
            await asyncio.sleep(0.3)  # longer than the patched handshake timeout
            return {"type": "auth_required"}

        mock_ws.receive_json = slow_receive
        mock_ws.send_json = AsyncMock()
        client._ws = mock_ws
        with (
            patch("backend.ha_client._HANDSHAKE_TIMEOUT_S", 0.05),
            pytest.raises(TimeoutError),
        ):
            await client._handshake()


# ---------------------------------------------------------------------------
# _subscribe_state_changed()
# ---------------------------------------------------------------------------


class TestSubscribeStateChanged:
    async def test_successful_subscribe(self):
        client = HAClient("ws://ha.local", "tok")
        mock_ws = AsyncMock()
        mock_ws.send_json = AsyncMock()
        mock_ws.receive_json = AsyncMock(return_value={"success": True, "id": 1})
        client._ws = mock_ws
        client._msg_id = 0
        await client._subscribe_state_changed()
        assert client._sub_id == 1
        mock_ws.send_json.assert_called_once()
        payload = mock_ws.send_json.call_args[0][0]
        assert payload["type"] == "subscribe_events"
        assert payload["event_type"] == "state_changed"

    async def test_subscribe_failure_raises(self):
        client = HAClient("ws://ha.local", "tok")
        mock_ws = AsyncMock()
        mock_ws.send_json = AsyncMock()
        mock_ws.receive_json = AsyncMock(return_value={"success": False, "error": "bad"})
        client._ws = mock_ws
        client._msg_id = 0
        # Must be a real exception, not an `assert` (stripped under python -O):
        # a rejected subscription must bounce start() back into its retry loop
        # instead of leaving a "connected" client that never hears an event.
        with pytest.raises(RuntimeError, match="Subscribe failed"):
            await client._subscribe_state_changed()
        assert client._sub_id is None

    async def test_subscribe_times_out_on_silent_peer(self):
        """Issue #297: the subscribe ack receive is timeout-guarded too."""
        client = HAClient("ws://ha.local", "tok")
        mock_ws = AsyncMock()
        mock_ws.send_json = AsyncMock()

        async def slow_receive():
            await asyncio.sleep(0.3)
            return {"success": True, "id": 1}

        mock_ws.receive_json = slow_receive
        client._ws = mock_ws
        client._msg_id = 0
        with (
            patch("backend.ha_client._HANDSHAKE_TIMEOUT_S", 0.05),
            pytest.raises(TimeoutError),
        ):
            await client._subscribe_state_changed()


# ---------------------------------------------------------------------------
# _read_loop()
# ---------------------------------------------------------------------------


class TestReadLoop:
    async def test_read_loop_processes_text_messages(self):
        client = HAClient("ws://ha.local", "tok")
        received = []

        async def fake_dispatch(data):
            received.append(data)

        client._dispatch = fake_dispatch

        payload = json.dumps({"type": "event", "data": {}})

        text_msg = MagicMock()
        text_msg.type = aiohttp.WSMsgType.TEXT
        text_msg.data = payload

        close_msg = MagicMock()
        close_msg.type = aiohttp.WSMsgType.CLOSED

        async def fake_iter():
            yield text_msg
            yield close_msg

        mock_ws = MagicMock()
        mock_ws.__aiter__ = lambda self: fake_iter()
        client._ws = mock_ws

        await client._read_loop()
        assert len(received) == 1

    async def test_read_loop_breaks_on_error_type(self):
        client = HAClient("ws://ha.local", "tok")

        error_msg = MagicMock()
        error_msg.type = aiohttp.WSMsgType.ERROR

        async def fake_iter():
            yield error_msg

        mock_ws = MagicMock()
        mock_ws.__aiter__ = lambda self: fake_iter()
        client._ws = mock_ws

        await client._read_loop()  # must not hang


# ---------------------------------------------------------------------------
# start() — tests the retry/backoff logic without opening a real connection
# ---------------------------------------------------------------------------


class TestStart:
    async def test_start_exits_when_running_set_false(self):
        client = HAClient("ws://ha.local", "tok")

        connect_calls = []

        async def fake_connect():
            connect_calls.append(1)
            client._running = False  # exit after first successful connect

        mock_connector = MagicMock()
        mock_session = AsyncMock()
        mock_session.close = AsyncMock()

        with (
            patch("aiohttp.TCPConnector", return_value=mock_connector),
            patch("aiohttp.ClientSession", return_value=mock_session),
        ):
            client._connect = fake_connect
            await client.start()

        assert len(connect_calls) == 1

    async def test_start_retries_on_connect_failure(self):
        client = HAClient("ws://ha.local", "tok")

        call_count = 0

        async def fail_then_stop():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("refused")
            client._running = False

        mock_connector = MagicMock()
        mock_session = AsyncMock()

        with (
            patch("aiohttp.TCPConnector", return_value=mock_connector),
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch("asyncio.sleep", AsyncMock()),
        ):
            client._connect = fail_then_stop
            await client.start()

        assert call_count == 2

    async def test_clean_close_paces_reconnect_without_busy_loop(self):
        """Issue #297: a clean close (read loop ending) must still pace the
        reconnect — a short-lived connection keeps the backoff growing instead
        of resetting to the minimum with zero delay."""
        client = HAClient("ws://ha.local", "tok")
        sleeps: list[float] = []
        count = 0

        async def quick_clean_connect():
            nonlocal count
            count += 1
            # The connection "comes up" but closes immediately (short session).
            client._connected_since = time.monotonic()
            if count >= 3:
                client._running = False

        async def fake_sleep(d):
            sleeps.append(d)

        with (
            patch("aiohttp.TCPConnector", return_value=MagicMock()),
            patch("aiohttp.ClientSession", return_value=AsyncMock()),
            patch("asyncio.sleep", fake_sleep),
        ):
            client._connect = quick_clean_connect
            await client.start()

        # Every clean close paced a non-zero sleep (no busy loop) and the backoff
        # grew because the connection never stayed up long enough to reset it.
        assert len(sleeps) == 2
        assert all(d > 0 for d in sleeps)
        assert sleeps[1] > sleeps[0]

    async def test_stable_connection_resets_backoff(self):
        """Issue #297: a connection that stayed up long enough resets the
        backoff to the minimum."""
        client = HAClient("ws://ha.local", "tok")
        sleeps: list[float] = []
        count = 0
        clock = [0.0]

        def fake_monotonic():
            return clock[0]

        async def long_lived_connect():
            nonlocal count
            count += 1
            client._connected_since = fake_monotonic()
            clock[0] += 100  # 100 s elapse during the session → well past stable
            if count >= 3:
                client._running = False

        async def fake_sleep(d):
            sleeps.append(d)

        with (
            patch("aiohttp.TCPConnector", return_value=MagicMock()),
            patch("aiohttp.ClientSession", return_value=AsyncMock()),
            patch("asyncio.sleep", fake_sleep),
            patch("time.monotonic", fake_monotonic),
        ):
            client._connect = long_lived_connect
            await client.start()

        # Each long-lived session reset the backoff, so every paced delay is the
        # minimum (never grows).
        assert sleeps
        assert all(d == sleeps[0] for d in sleeps)

    async def test_start_ssl_verify_false_passes_false_to_connector(self):
        client = HAClient("ws://ha.local", "tok", ssl_verify=False)
        client._running = False  # don't loop

        captured_ssl = []

        def patched_connector(**kwargs):
            captured_ssl.append(kwargs.get("ssl"))
            mock = MagicMock()
            return mock

        mock_session = AsyncMock()

        with (
            patch("aiohttp.TCPConnector", side_effect=patched_connector),
            patch("aiohttp.ClientSession", return_value=mock_session),
        ):

            async def noop_connect():
                client._running = False

            client._connect = noop_connect
            await client.start()

        assert False in captured_ssl


# ---------------------------------------------------------------------------
# _connect()
# ---------------------------------------------------------------------------


class TestConnect:
    async def test_connect_success_path(self):
        client = HAClient("ws://ha.local", "tok")
        connected_during_read: list[bool] = []

        client._handshake = AsyncMock()
        client._subscribe_state_changed = AsyncMock()
        client.fetch_states = AsyncMock(return_value=[])
        client.get_temperature_unit = AsyncMock(return_value="F")

        async def capturing_read_loop():
            connected_during_read.append(client._connected.is_set())

        client._read_loop = capturing_read_loop

        mock_ws = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.ws_connect = MagicMock(return_value=mock_ctx)
        client._session = mock_session

        await client._connect()

        client._handshake.assert_called_once()
        client._subscribe_state_changed.assert_called_once()
        # _connected must have been set while the read loop ran
        assert connected_during_read == [True]
        # _connected must be cleared after _connect() returns (finally block)
        assert not client._connected.is_set()

    async def test_connect_passes_heartbeat_to_ws_connect(self):
        """Issue #297: ws_connect must be given a heartbeat so a silently
        half-open socket is detected."""
        client = HAClient("ws://ha.local", "tok")
        client._handshake = AsyncMock()
        client._subscribe_state_changed = AsyncMock()
        client.fetch_states = AsyncMock(return_value=[])
        client.get_temperature_unit = AsyncMock(return_value="F")
        client._read_loop = AsyncMock()

        mock_ws = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.ws_connect = MagicMock(return_value=mock_ctx)
        client._session = mock_session

        await client._connect()

        _, kwargs = mock_session.ws_connect.call_args
        assert kwargs.get("heartbeat") is not None

    async def test_connect_fetch_states_failure_is_swallowed(self):
        client = HAClient("ws://ha.local", "tok")
        connected_during_read: list[bool] = []

        client._handshake = AsyncMock()
        client._subscribe_state_changed = AsyncMock()
        client.fetch_states = AsyncMock(side_effect=RuntimeError("http error"))
        client.get_temperature_unit = AsyncMock(return_value="F")

        async def capturing_read_loop():
            connected_during_read.append(client._connected.is_set())

        client._read_loop = capturing_read_loop

        mock_ws = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.ws_connect = MagicMock(return_value=mock_ctx)
        client._session = mock_session

        await client._connect()  # must not raise

        # fetch_states failure is swallowed; connection still proceeds
        assert connected_during_read == [True]
        assert not client._connected.is_set()


# ---------------------------------------------------------------------------
# HAClient.get_temperature_unit()
# ---------------------------------------------------------------------------


class TestGetTemperatureUnit:
    """HA's /api/config returns ``unit_system`` as an OBJECT, e.g.
    ``{"length": "km", "temperature": "°C", ...}`` — never the bare string
    ``"metric"``/``"imperial"``. These fixtures use the real object shape so the
    parsing is exercised the way HA actually responds. (Issue #281)
    """

    @staticmethod
    def _client_with_config(cfg: object) -> HAClient:
        client = HAClient("ws://ha.local", "tok")
        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value=cfg)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session
        return client

    async def test_imperial_returns_F(self):
        client = self._client_with_config({"unit_system": {"temperature": "°F", "length": "mi"}})
        assert await client.get_temperature_unit() == "F"

    async def test_metric_returns_C(self):
        client = self._client_with_config({"unit_system": {"temperature": "°C", "length": "km"}})
        assert await client.get_temperature_unit() == "C"

    async def test_unknown_temperature_defaults_to_F(self):
        client = self._client_with_config({"unit_system": {"temperature": "?"}})
        assert await client.get_temperature_unit() == "F"

    async def test_missing_unit_system_defaults_to_F(self):
        client = self._client_with_config({})
        assert await client.get_temperature_unit() == "F"

    async def test_legacy_string_shape_defaults_to_F(self):
        # Defensive: a non-dict unit_system (legacy/misconfigured) must not crash
        # and falls back to °F rather than mis-detecting.
        client = self._client_with_config({"unit_system": "metric"})
        assert await client.get_temperature_unit() == "F"

    async def test_result_is_cached_on_ha_temp_unit(self):
        client = self._client_with_config({"unit_system": {"temperature": "°C", "length": "km"}})
        assert client.ha_temp_unit == "F"  # default before resolution
        await client.get_temperature_unit()
        assert client.ha_temp_unit == "C"  # cached for the climate read/write path

    async def test_wss_url_converted_to_https(self):
        client = HAClient("wss://ha.example.com", "tok")
        captured_urls = []
        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"unit_system": {"temperature": "°F"}})

        def capture_get(url, **kwargs):
            captured_urls.append(url)
            return mock_resp

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=capture_get)
        client._session = mock_session
        await client.get_temperature_unit()
        assert captured_urls[0].startswith("https://")


# ---------------------------------------------------------------------------
# _send() disconnect fast-fail and pending cleanup
# ---------------------------------------------------------------------------


class TestSend:
    async def test_send_raises_when_not_connected(self):
        client = HAClient("ws://ha.local", "tok")
        # _connected is not set — should raise immediately
        with pytest.raises(RuntimeError, match="HA not connected"):
            await client._send({"type": "call_service"})

    async def test_send_cancels_pending_on_send_json_failure(self):
        client = HAClient("ws://ha.local", "tok")
        client._connected.set()

        mock_ws = AsyncMock()
        mock_ws.send_json.side_effect = RuntimeError("send failed")
        client._ws = mock_ws

        with pytest.raises(RuntimeError, match="send failed"):
            await client._send({"type": "call_service"})

        # Pending future must have been cleaned up
        assert client._pending == {}

    async def test_connect_cleanup_cancels_pending_futures(self):
        """Pending futures are resolved with an error when _connect exits."""
        client = HAClient("ws://ha.local", "tok")

        async def slow_read_loop():
            # While connected, register a pending future then let the loop exit
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            client._pending[99] = fut
            await asyncio.sleep(0)  # yield so finally runs when we exit

        client._handshake = AsyncMock()
        client._subscribe_state_changed = AsyncMock()
        client.fetch_states = AsyncMock(return_value=[])
        client._read_loop = slow_read_loop

        mock_ws = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.ws_connect = MagicMock(return_value=mock_ctx)
        client._session = mock_session

        # Manually plant a pending future before calling _connect
        loop = asyncio.get_running_loop()
        orphan_fut = loop.create_future()
        client._pending[42] = orphan_fut

        await client._connect()

        # After _connect exits, all pending futures must be resolved and cleared
        assert client._pending == {}
        assert orphan_fut.done()
        exc = orphan_fut.exception()
        assert exc is not None
        assert "disconnected" in str(exc).lower()


class TestListenerTaskRetention:
    """#431: listener-dispatch tasks must be strongly referenced until they
    finish (the event loop keeps only weak refs — the #304 GC bug class), and
    listener exceptions must be logged instead of vanishing."""

    @pytest.mark.asyncio
    async def test_dispatch_retains_tasks_until_done(self):
        client = HAClient("http://ha:8123", "token")
        started = asyncio.Event()
        release = asyncio.Event()
        seen: list[str] = []

        async def listener(entity_id: str, state: dict) -> None:
            started.set()
            await release.wait()
            seen.append(entity_id)

        client.subscribe_all(listener)
        await client._dispatch(
            {
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {
                        "entity_id": "sensor.x",
                        "new_state": {"entity_id": "sensor.x", "state": "1"},
                    },
                },
            }
        )
        await started.wait()
        assert client._dispatch_tasks, "in-flight listener task must be strongly referenced"
        release.set()
        await asyncio.gather(*client._dispatch_tasks)
        await asyncio.sleep(0)
        assert not client._dispatch_tasks, "finished tasks must be released"
        assert seen == ["sensor.x"]

    @pytest.mark.asyncio
    async def test_listener_exception_is_logged_not_silent(self, caplog):
        client = HAClient("http://ha:8123", "token")

        async def bad_listener(entity_id: str, state: dict) -> None:
            raise RuntimeError("listener boom")

        client.subscribe("sensor.x", bad_listener)
        with caplog.at_level("ERROR"):
            await client._dispatch(
                {
                    "type": "event",
                    "event": {
                        "event_type": "state_changed",
                        "data": {
                            "entity_id": "sensor.x",
                            "new_state": {"entity_id": "sensor.x", "state": "1"},
                        },
                    },
                }
            )
            # Let the task run and the done-callback fire.
            for _ in range(5):
                await asyncio.sleep(0)
        assert any("listener raised" in r.message for r in caplog.records)
