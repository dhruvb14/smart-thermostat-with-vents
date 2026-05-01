"""
Tests for HAClient internal connection methods (_handshake, _subscribe_state_changed,
_read_loop) and the start() retry loop. These target ha_client.py lines that are
only exercised by the real WebSocket lifecycle.
"""

from __future__ import annotations

import json
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
        with pytest.raises(AssertionError):
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

        client._handshake = AsyncMock()
        client._subscribe_state_changed = AsyncMock()
        client._read_loop = AsyncMock()
        client.fetch_states = AsyncMock(return_value=[])

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
        client._read_loop.assert_called_once()
        assert client._connected.is_set()

    async def test_connect_fetch_states_failure_is_swallowed(self):
        client = HAClient("ws://ha.local", "tok")

        client._handshake = AsyncMock()
        client._subscribe_state_changed = AsyncMock()
        client._read_loop = AsyncMock()
        client.fetch_states = AsyncMock(side_effect=RuntimeError("http error"))

        mock_ws = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.ws_connect = MagicMock(return_value=mock_ctx)
        client._session = mock_session

        await client._connect()  # must not raise

        assert client._connected.is_set()
