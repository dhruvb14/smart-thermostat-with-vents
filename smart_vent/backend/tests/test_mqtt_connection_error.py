"""``describe_connection_error`` — what the Settings MQTT card shows (alert #4).

``GET /api/settings/mqtt`` returns the bridge's ``last_error``. It used to be
``f"{type(exc).__name__}: {exc}"`` — raw exception text in an API response
(CWE-209). It is now always one of a fixed set of strings chosen by exception
*type*, and these tests pin that no exception message ever leaks through.
"""

from __future__ import annotations

import socket
import ssl

import aiomqtt
import pytest
from aiomqtt.exceptions import MqttConnectError
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from backend.mqtt import bridge as bridge_mod
from backend.mqtt.bridge import describe_connection_error

SECRET = "broker.internal.example:8883 secret-detail"


def _wrapped(inner: BaseException) -> aiomqtt.MqttError:
    """Mimic aiomqtt's ``raise MqttError(str(exc)) from None``."""
    try:
        try:
            raise inner
        except OSError as exc:
            raise aiomqtt.MqttError(str(exc)) from None
    except aiomqtt.MqttError as outer:
        return outer


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (MqttConnectError(4), bridge_mod.CONNECT_ERROR_AUTH),
        (MqttConnectError(5), bridge_mod.CONNECT_ERROR_AUTH),
        (
            MqttConnectError(ReasonCode(PacketTypes.CONNACK, identifier=135)),
            bridge_mod.CONNECT_ERROR_AUTH,
        ),
        (MqttConnectError(3), bridge_mod.CONNECT_ERROR_REFUSED_BY_BROKER),
        (
            MqttConnectError(ReasonCode(PacketTypes.CONNACK, identifier=136)),
            bridge_mod.CONNECT_ERROR_REFUSED_BY_BROKER,
        ),
        (_wrapped(socket.gaierror(-2, SECRET)), bridge_mod.CONNECT_ERROR_DNS),
        (_wrapped(ConnectionRefusedError(111, SECRET)), bridge_mod.CONNECT_ERROR_PORT_CLOSED),
        (_wrapped(TimeoutError(SECRET)), bridge_mod.CONNECT_ERROR_TIMEOUT),
        (_wrapped(ssl.SSLError(1, SECRET)), bridge_mod.CONNECT_ERROR_TLS),
        (_wrapped(OSError(113, SECRET)), bridge_mod.CONNECT_ERROR_NETWORK),
        (OSError(SECRET), bridge_mod.CONNECT_ERROR_NETWORK),
        (RuntimeError(SECRET), bridge_mod.CONNECT_ERROR_OTHER),
    ],
)
def test_classifies_by_type_and_never_echoes_the_message(exc, expected) -> None:
    text = describe_connection_error(exc, was_connected=False)
    assert text == expected
    assert SECRET not in text


def test_explicit_cause_is_searched_too() -> None:
    outer = RuntimeError(SECRET)
    outer.__cause__ = socket.gaierror(-2, SECRET)
    assert describe_connection_error(outer, was_connected=False) == bridge_mod.CONNECT_ERROR_DNS


@pytest.mark.parametrize("exc", [aiomqtt.MqttError(SECRET), OSError(SECRET)])
def test_a_drop_after_connecting_reads_as_lost(exc) -> None:
    assert describe_connection_error(exc, was_connected=True) == bridge_mod.CONNECT_ERROR_LOST


def test_a_timeout_after_connecting_still_reads_as_a_timeout() -> None:
    exc = TimeoutError(SECRET)
    assert describe_connection_error(exc, was_connected=True) == bridge_mod.CONNECT_ERROR_TIMEOUT
